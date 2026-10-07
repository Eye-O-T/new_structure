# Nginx의 영상 열람 확인과 MediaMTX의 송출·읽기 인증 요청을 대신 판단한다.
from __future__ import annotations

import asyncio
import hmac
import logging
import re
import secrets
import time
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
    camera_admission_lock,
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
LOGGER = logging.getLogger("ai_cctv.external.media_auth")


DUMMY_MEDIA_PASSWORD_HASH = hash_password("invalid-media-credential")


class _MediaAuthResponse(Response):
    """MediaMTX에 인증 응답을 보낸 뒤 카메라 잠금을 해제한다."""

    def __init__(
        self,
        lock: asyncio.Lock,
        *,
        request_id: str,
        action: str,
        protocol: str,
        camera_id: str,
        started_at: float,
        lock_acquired_at: float,
    ) -> None:
        super().__init__(status_code=204)
        self._camera_lock = lock
        self._request_id = request_id
        self._action = action
        self._protocol = protocol
        self._camera_id = camera_id
        self._started_at = started_at
        self._lock_acquired_at = lock_acquired_at

    # 성공 응답 전송이 끝날 때까지 잠금을 보유하고 전송 예외가 발생해도 해제한다.
    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        except BaseException as exc:
            LOGGER.warning(
                "MEDIA_AUTH_RESPONSE_FAILED request_id=%s action=%s protocol=%s "
                "camera_id=%s error_type=%s total_ms=%.1f",
                self._request_id,
                self._action,
                self._protocol,
                self._camera_id,
                type(exc).__name__,
                (time.monotonic() - self._started_at) * 1000,
            )
            raise
        else:
            LOGGER.info(
                "MEDIA_AUTH_RESPONSE_SENT request_id=%s action=%s protocol=%s "
                "camera_id=%s status=204 total_ms=%.1f",
                self._request_id,
                self._action,
                self._protocol,
                self._camera_id,
                (time.monotonic() - self._started_at) * 1000,
            )
        finally:
            self._camera_lock.release()
            LOGGER.info(
                "MEDIA_AUTH_LOCK_RELEASED request_id=%s camera_id=%s held_ms=%.1f",
                self._request_id,
                self._camera_id,
                (time.monotonic() - self._lock_acquired_at) * 1000,
            )


# 원래 영상 URI와 요청 선택자의 일치를 확인하고 해당 카메라·녹화·이벤트의 권한을 검사한다.
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
        # 인증 요청은 보호된 두 영상 경로에만 허용한다. Nginx의 정규화와 해석이 달라질 URI는 거절한다.
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


# 내부 HLS·RTSP 읽기·카메라 송출의 인증 경로를 나누고 송출 상태 확인을 잠금으로 보호한다.
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
        # Playback/API/metrics/pprof는 MediaMTX 인증 대상에서 제외되며 내부 Compose망에만 있다.
        return Response(status_code=204)
    started_at = time.monotonic()
    request_id = secrets.token_hex(4)
    safe_camera_id = (
        payload.path if CAMERA_ID_PATTERN.fullmatch(payload.path) else "<invalid>"
    )
    LOGGER.info(
        "MEDIA_AUTH_RECEIVED request_id=%s action=%s protocol=%s camera_id=%s",
        request_id,
        action,
        protocol,
        safe_camera_id,
    )
    if not CAMERA_ID_PATTERN.fullmatch(payload.path):
        LOGGER.warning(
            "MEDIA_AUTH_REJECTED request_id=%s action=%s protocol=%s "
            "camera_id=%s reason=invalid_camera_id total_ms=%.1f",
            request_id,
            action,
            protocol,
            safe_camera_id,
            (time.monotonic() - started_at) * 1000,
        )
        raise _auth_error("Media authentication failed")
    if action == "read" and protocol != "rtsp":
        LOGGER.warning(
            "MEDIA_AUTH_REJECTED request_id=%s action=%s protocol=%s "
            "camera_id=%s reason=invalid_read_protocol total_ms=%.1f",
            request_id,
            action,
            protocol,
            safe_camera_id,
            (time.monotonic() - started_at) * 1000,
        )
        raise _auth_error("Media authentication failed")

    # 카메라 활성 상태 확인부터 인증 응답 전송까지 같은 잠금을 잡는다.
    # 비활성화·키 교체·삭제 도중에 예전 인증으로 새 송출 연결이 끼어드는 것을 막기 위해서다.
    camera_lock = camera_admission_lock(request, payload.path)
    lock_wait_started_at = time.monotonic()
    LOGGER.info(
        "MEDIA_AUTH_LOCK_WAIT request_id=%s camera_id=%s",
        request_id,
        safe_camera_id,
    )
    try:
        await camera_lock.acquire()
    except BaseException as exc:
        LOGGER.warning(
            "MEDIA_AUTH_LOCK_WAIT_FAILED request_id=%s camera_id=%s "
            "error_type=%s wait_ms=%.1f total_ms=%.1f",
            request_id,
            safe_camera_id,
            type(exc).__name__,
            (time.monotonic() - lock_wait_started_at) * 1000,
            (time.monotonic() - started_at) * 1000,
        )
        raise
    lock_acquired_at = time.monotonic()
    LOGGER.info(
        "MEDIA_AUTH_LOCK_ACQUIRED request_id=%s camera_id=%s wait_ms=%.1f",
        request_id,
        safe_camera_id,
        (lock_acquired_at - lock_wait_started_at) * 1000,
    )
    try:
        data_lookup_started_at = time.monotonic()
        LOGGER.info(
            "MEDIA_AUTH_CAMERA_LOOKUP_START request_id=%s camera_id=%s",
            request_id,
            safe_camera_id,
        )
        try:
            camera = await data.get_camera(payload.path, user_id="0")
        except DataNotFound as exc:
            LOGGER.warning(
                "MEDIA_AUTH_REJECTED request_id=%s action=%s protocol=%s "
                "camera_id=%s reason=camera_not_found data_ms=%.1f total_ms=%.1f",
                request_id,
                action,
                protocol,
                safe_camera_id,
                (time.monotonic() - data_lookup_started_at) * 1000,
                (time.monotonic() - started_at) * 1000,
            )
            raise _auth_error("Media authentication failed") from exc
        except BaseException as exc:
            LOGGER.warning(
                "MEDIA_AUTH_CAMERA_LOOKUP_FAILED request_id=%s camera_id=%s "
                "error_type=%s data_ms=%.1f total_ms=%.1f",
                request_id,
                safe_camera_id,
                type(exc).__name__,
                (time.monotonic() - data_lookup_started_at) * 1000,
                (time.monotonic() - started_at) * 1000,
            )
            raise
        LOGGER.info(
            "MEDIA_AUTH_CAMERA_LOOKUP_COMPLETE request_id=%s camera_id=%s "
            "enabled=%s data_ms=%.1f",
            request_id,
            safe_camera_id,
            bool(camera.get("enabled", True)),
            (time.monotonic() - data_lookup_started_at) * 1000,
        )
        if not bool(camera.get("enabled", True)):
            LOGGER.warning(
                "MEDIA_AUTH_REJECTED request_id=%s action=%s protocol=%s "
                "camera_id=%s reason=camera_disabled total_ms=%.1f",
                request_id,
                action,
                protocol,
                safe_camera_id,
                (time.monotonic() - started_at) * 1000,
            )
            raise _auth_error("Media authentication failed")

        if action == "read":
            username_valid = hmac.compare_digest(
                payload.user, settings.media_read_username
            )
            password_valid = hmac.compare_digest(
                payload.password, settings.media_read_password
            )
        else:
            # 등록·교체 시 발급한 DB 인증값을 우선한다. 정적 인증값은 DB 저장 도입 전 카메라의 초기 대체값이다.
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
                if expected is None:
                    # 등록 누락은 빈 비밀번호 계정이 아니다. 원문 인증값 없이 운영 진단을 남긴다.
                    LOGGER.warning(
                        "PUBLISH_CREDENTIAL_MISSING camera_id=%s", payload.path
                    )
                    raise _auth_error("Media authentication failed")
                expected_username = expected.username
                expected_password = expected.password
                username_valid = hmac.compare_digest(payload.user, expected_username)
                password_valid = hmac.compare_digest(
                    payload.password, expected_password
                )
        if not (username_valid and password_valid):
            LOGGER.warning(
                "MEDIA_AUTH_REJECTED request_id=%s action=%s protocol=%s "
                "camera_id=%s reason=credential_mismatch total_ms=%.1f",
                request_id,
                action,
                protocol,
                safe_camera_id,
                (time.monotonic() - started_at) * 1000,
            )
            raise _auth_error("Media authentication failed")
    except BaseException as exc:
        camera_lock.release()
        if not isinstance(exc, HTTPException):
            LOGGER.warning(
                "MEDIA_AUTH_ABORTED request_id=%s action=%s protocol=%s camera_id=%s "
                "error_type=%s total_ms=%.1f lock_held_ms=%.1f",
                request_id,
                action,
                protocol,
                safe_camera_id,
                type(exc).__name__,
                (time.monotonic() - started_at) * 1000,
                (time.monotonic() - lock_acquired_at) * 1000,
            )
        raise
    LOGGER.info(
        "MEDIA_AUTH_ACCEPTED request_id=%s action=%s protocol=%s camera_id=%s "
        "auth_ms=%.1f",
        request_id,
        action,
        protocol,
        safe_camera_id,
        (time.monotonic() - started_at) * 1000,
    )
    return _MediaAuthResponse(
        camera_lock,
        request_id=request_id,
        action=action,
        protocol=protocol,
        camera_id=safe_camera_id,
        started_at=started_at,
        lock_acquired_at=lock_acquired_at,
    )
