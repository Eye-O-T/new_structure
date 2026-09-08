# Nginx의 영상 열람 확인과 MediaMTX의 송출·읽기 인증 요청을 대신 판단한다.
from __future__ import annotations

import asyncio
import hmac
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
)

from ..clients.data import (
    DataClient,
    DataForbidden,
    DataNotFound,
)
from ..config import CAMERA_ID_PATTERN, Settings
from ..dependencies import (
    camera_lifecycle_lock,
    get_data_client,
    get_settings_dependency,
)
from ..schemas import (
    AuthVerifyRequest,
    MediaAuthRequest,
)
from ..security.passwords import hash_password, verify_password
from ..security.permissions import (
    Principal,
    _ensure_camera_access,
    get_current_principal,
)
from .auth import _auth_error
from .validation import _validate_resource_id

router = APIRouter()


DUMMY_MEDIA_PASSWORD_HASH = hash_password("invalid-media-credential")


class _MediaAuthResponse(Response):
    """Release a camera lock only after MediaMTX receives the auth response."""

    def __init__(self, lock: asyncio.Lock) -> None:
        super().__init__(status_code=204)
        self._camera_lock = lock

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._camera_lock.release()


@router.get(
    "/internal/auth/verify",
    operation_id="verify_internal_auth_get",
    include_in_schema=False,
)
@router.post(
    "/internal/auth/verify",
    operation_id="verify_internal_auth_post",
    include_in_schema=False,
)
async def internal_auth_verify(
    request: Request,
    response: Response,
    payload: AuthVerifyRequest | None = Body(default=None),
    camera_id: str | None = Query(default=None),
    principal: Principal = Depends(get_current_principal),
    data: DataClient = Depends(get_data_client),
) -> dict[str, bool]:
    selected_camera_id = camera_id or request.headers.get("X-Camera-ID")
    resource_type = payload.resource_type if payload is not None else None
    resource_id = payload.resource_id if payload is not None else None
    if payload is not None and payload.camera_id is not None:
        selected_camera_id = payload.camera_id

    original_uri = request.headers.get("X-Original-URI", "")
    parsed_original_uri = urlsplit(original_uri)
    original_path = parsed_original_uri.path
    decoded_original_path = unquote(original_path)
    if original_uri and (
        # Nginx와 MediaMTX가 경로를 다르게 해석하면 권한 검사를 우회할 수 있다.
        # 인코딩·중복 구분자처럼 해석이 모호한 경로는 카메라를 판단하기 전에 거절한다.
        "%" in original_path
        or "\\" in decoded_original_path
        or "//" in decoded_original_path
        or any(part in {".", ".."} for part in decoded_original_path.split("/"))
    ):
        raise HTTPException(status_code=400, detail="Invalid protected path")
    original_query = parse_qs(parsed_original_uri.query, keep_blank_values=True)
    hls_match = re.fullmatch(
        rf"/hls/({CAMERA_ID_PATTERN.pattern[1:-1]})(?:/.*)?",
        original_path,
    )
    if hls_match is not None:
        uri_camera_id = hls_match.group(1)
        if selected_camera_id is not None and selected_camera_id != uri_camera_id:
            raise HTTPException(
                status_code=400, detail="Camera selector conflicts with media URI"
            )
        selected_camera_id = uri_camera_id
    if original_path.startswith("/hls/") and hls_match is None:
        raise HTTPException(status_code=400, detail="Invalid HLS path")
    if original_path in {
        "/playback/get",
        "/playback/list",
    }:
        path_values = original_query.get("path", [])
        if len(path_values) != 1:
            raise HTTPException(status_code=400, detail="Playback path is required")
        uri_camera_id = path_values[0]
        if selected_camera_id is not None and selected_camera_id != uri_camera_id:
            raise HTTPException(
                status_code=400, detail="Camera selector conflicts with media URI"
            )
        selected_camera_id = uri_camera_id
    elif original_path.startswith("/playback/"):
        raise HTTPException(status_code=400, detail="Invalid playback path")
    elif original_uri and not original_path.startswith("/hls/"):
        # auth_request is valid only for the two protected media
        # namespaces. Never approve a raw URI that Nginx may have
        # normalized into one of them while this service saw another.
        raise HTTPException(status_code=400, detail="Invalid protected path")

    if selected_camera_id is not None:
        camera = await _ensure_camera_access(data, principal, selected_camera_id)
        if original_path.startswith("/hls/") and not bool(camera.get("enabled", True)):
            raise DataForbidden("disabled camera has no live stream")
    elif resource_type is not None:
        if resource_id is None:
            raise HTTPException(status_code=400, detail="resource_id is required")
        resource_id = _validate_resource_id(resource_id)
        if resource_type == "camera":
            await _ensure_camera_access(data, principal, resource_id)
        elif resource_type == "recording":
            recording = await data.get_recording(
                resource_id,
                user_id=principal.user_id,
            )
            await _ensure_camera_access(
                data,
                principal,
                str(recording.get("camera_id", "")),
            )
        elif resource_type == "event":
            event = await data.get_event(resource_id, user_id=principal.user_id)
            await _ensure_camera_access(
                data,
                principal,
                str(event.get("camera_id", "")),
            )

    response.headers["X-User-ID"] = principal.user_id
    response.headers["X-User-Role"] = principal.role
    return {"valid": True}


@router.post("/internal/media-auth", status_code=204, include_in_schema=False)
async def internal_media_auth(
    request: Request,
    payload: MediaAuthRequest,
    settings: Settings = Depends(get_settings_dependency),
    data: DataClient = Depends(get_data_client),
) -> Response:
    action = payload.action.lower()
    protocol = payload.protocol.lower()
    if action == "read" and protocol == "hls":
        # 현재 배포의 공개 HLS 요청은 Nginx에서 JWT와 카메라 권한을 먼저 검사한다.
        # 이 허용은 HLS 포트를 외부에 직접 열지 않는 Compose 구성에 의존한다.
        return Response(status_code=204)
    if action not in {"publish", "read"}:
        # Playback/API/metrics/pprof are excluded by MediaMTX config and
        # remain on the private Compose network.
        return Response(status_code=204)
    if not CAMERA_ID_PATTERN.fullmatch(payload.path):
        raise _auth_error("Media authentication failed")
    if action == "read" and protocol != "rtsp":
        raise _auth_error("Media authentication failed")

    # 카메라 활성 상태 확인부터 인증 응답 전송까지 같은 잠금을 잡는다.
    # 비활성화·키 교체·삭제 도중에 예전 인증으로 새 송출 연결이 끼어드는 것을 막기 위해서다.
    camera_lock = camera_lifecycle_lock(request, payload.path)
    await camera_lock.acquire()
    try:
        try:
            camera = await data.get_camera(payload.path, user_id="0")
        except DataNotFound as exc:
            raise _auth_error("Media authentication failed") from exc
        if not bool(camera.get("enabled", True)):
            raise _auth_error("Media authentication failed")

        if action == "read":
            username_valid = hmac.compare_digest(
                payload.user, settings.media_read_username
            )
            password_valid = hmac.compare_digest(
                payload.password, settings.media_read_password
            )
        else:
            # A database credential is issued on every registration/rotation
            # and is authoritative. Static credentials remain a bootstrap-only
            # fallback for cameras created before persistence existed.
            dynamic = await data.get_camera_publish_credential(payload.path)
            if dynamic is not None:
                expected_username = str((dynamic or {}).get("username", ""))
                password_hash = str(
                    (dynamic or {}).get("password_hash", DUMMY_MEDIA_PASSWORD_HASH)
                )
                username_valid = hmac.compare_digest(payload.user, expected_username)
                password_valid = verify_password(password_hash, payload.password)
            else:
                expected = settings.media_publish_credentials.get(payload.path)
                expected_username = expected.username if expected is not None else ""
                expected_password = expected.password if expected is not None else ""
                username_valid = hmac.compare_digest(payload.user, expected_username)
                password_valid = hmac.compare_digest(
                    payload.password, expected_password
                )
        if not (username_valid and password_valid):
            raise _auth_error("Media authentication failed")
    except BaseException:
        camera_lock.release()
        raise
    return _MediaAuthResponse(camera_lock)
