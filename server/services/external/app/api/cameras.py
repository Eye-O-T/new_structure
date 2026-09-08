# 카메라 설정과 영상 조회를 처리하며 Data·Edge·MediaMTX 작업 순서를 조정한다.
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote, urlsplit

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    Response,
)

from ..clients.data import (
    DataClient,
    DataConflict,
    DataServiceError,
)
from ..clients.edge import EdgeControlError, EdgeHttpClient
from ..clients.mediamtx import MediaMtxClient
from ..config import CAMERA_ID_PATTERN, Settings
from ..dependencies import (
    camera_lifecycle_lock,
    get_data_client,
    get_settings_dependency,
    hold_camera_lifecycle_lock,
)
from ..schemas import (
    CameraCreate,
    CameraLiveResponse,
    CameraPageResponse,
    CameraPatch,
    CameraResponse,
    CameraStatusResponse,
    VideoProfilePatch,
    VideoProfileResponse,
)
from ..security.passwords import hash_password
from ..security.permissions import (
    Principal,
    _ensure_camera_access,
    get_current_principal,
    require_admin,
)
from .validation import _public_media_url

router = APIRouter()


async def _disconnect_camera_publisher(settings: Settings, camera_id: str) -> bool:
    client = MediaMtxClient(
        settings.media_control_url,
        timeout_seconds=settings.media_control_timeout_seconds,
    )
    try:
        return await client.disconnect_publisher(camera_id)
    finally:
        await client.close()


def _public_camera(camera: dict[str, Any]) -> dict[str, Any]:
    result = dict(camera)
    for internal_field in (
        "source_url",
        "edge_device_id",
        "edge_management_url",
        "edge_recovery_url",
        "edge_auth_token",
        "management_url",
        "recovery_url",
        "auth_token",
        # cameras.status is a schema-v1 compatibility column. Runtime state is
        # served exclusively by /cameras/{id}/status.
        "status",
    ):
        result.pop(internal_field, None)
    return result


def _public_cameras(payload: Any) -> Any:
    if isinstance(payload, list):
        return [_public_camera(item) for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        result = dict(payload)
        result["items"] = [
            _public_camera(item) for item in payload["items"] if isinstance(item, dict)
        ]
        return result
    if isinstance(payload, dict):
        return _public_camera(payload)
    raise DataServiceError("invalid camera response")


def _validate_source_url(value: str | None) -> None:
    if value is None:
        return
    parsed = urlsplit(value)
    if parsed.scheme not in {"rtsp", "rtsps"} or not parsed.hostname:
        raise HTTPException(
            status_code=400, detail="source_url must be a valid RTSP URL"
        )
    if any(ord(character) < 32 for character in value):
        raise HTTPException(status_code=400, detail="source_url is invalid")


def _validate_edge_url(value: str | None, name: str) -> None:
    if value is None:
        return
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(part == ".." for part in parsed.path.split("/"))
    ):
        raise HTTPException(
            status_code=400, detail=f"{name} must be a credential-free HTTP(S) URL"
        )


def _edge_capability_error(
    capabilities: dict[str, Any],
) -> EdgeControlError | None:
    capability_status = capabilities.get("capability_status")
    if capability_status is not None and capability_status not in {
        "available",
        "unavailable",
        "unknown",
    }:
        return EdgeControlError(
            "INVALID_EDGE_RESPONSE",
            "The Edge device returned an invalid capability status.",
        )

    for field in ("camera_available", "encoder_available"):
        value = capabilities.get(field)
        if (
            field in capabilities
            and value is not True
            and value is not False
            and value is not None
        ):
            return EdgeControlError(
                "INVALID_EDGE_RESPONSE",
                f"The Edge device returned an invalid {field} flag.",
            )

    details = {
        field: capabilities.get(field)
        for field in (
            "capability_status",
            "camera_available",
            "encoder_available",
        )
        if field in capabilities
    }
    if capabilities.get("camera_available") is False:
        return EdgeControlError(
            "CAMERA_UNAVAILABLE",
            "The Edge camera input is unavailable.",
            status_code=409,
            details=details,
        )
    if capabilities.get("encoder_available") is False:
        return EdgeControlError(
            "ENCODER_UNAVAILABLE",
            "The Edge video encoder is unavailable.",
            status_code=409,
            details=details,
        )

    explicit_unknown_flag = any(
        field in capabilities and capabilities[field] is None
        for field in ("camera_available", "encoder_available")
    )
    if capability_status in {"unknown", "unavailable"} or explicit_unknown_flag:
        return EdgeControlError(
            "CAPABILITY_UNKNOWN",
            "The Edge camera or encoder capability could not be verified.",
            status_code=409,
            details=details,
        )
    return None


@router.get("/api/v1/cameras", response_model=CameraPageResponse)
async def list_cameras(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_current_principal),
    data: DataClient = Depends(get_data_client),
) -> Any:
    return _public_cameras(
        await data.list_cameras(
            user_id=principal.user_id,
            limit=limit,
            offset=offset,
        )
    )


@router.post("/api/v1/cameras", status_code=201, response_model=CameraResponse)
async def create_camera(
    request: Request,
    payload: CameraCreate,
    response: Response,
    _: Principal = Depends(require_admin),
    settings: Settings = Depends(get_settings_dependency),
    data: DataClient = Depends(get_data_client),
) -> Any:
    _validate_source_url(payload.source_url)
    _validate_edge_url(payload.edge_management_url, "edge_management_url")
    _validate_edge_url(payload.edge_recovery_url, "edge_recovery_url")
    if payload.stream_path is not None and payload.stream_path != payload.camera_id:
        raise HTTPException(status_code=400, detail="stream_path must match camera_id")
    body = payload.model_dump(exclude_none=True, exclude={"edge_auth_token"})
    if payload.edge_auth_token is not None:
        body["edge_auth_token"] = payload.edge_auth_token.get_secret_value()
    body["stream_path"] = payload.camera_id
    requested_enabled = bool(body.get("enabled", True))
    # Keep admission closed until the authoritative DB credential exists.
    # Otherwise a deleted/re-registered bootstrap camera can briefly fall
    # back to its stale static credential between these two Data calls.
    create_body = {
        **body,
        "enabled": False,
        "status": "disabled",
    }
    async with camera_lifecycle_lock(request, payload.camera_id):
        camera = await data.create_camera(create_body)
        publish_password = secrets.token_urlsafe(32)
        try:
            await data.put_camera_publish_credential(
                payload.camera_id,
                {
                    "username": payload.camera_id,
                    "password_hash": hash_password(publish_password),
                },
            )
            if requested_enabled:
                camera = await data.update_camera(
                    payload.camera_id,
                    {"enabled": True, "status": "offline"},
                )
        except Exception:
            # Keep the disabled row in place until any old publisher is gone;
            # deleting first would make the static bootstrap fallback valid
            # again while rollback is still in progress. If MediaMTX cannot be
            # reached, leave the fail-closed row for an administrator to retry.
            await _disconnect_camera_publisher(settings, payload.camera_id)
            await data.delete_camera(payload.camera_id)
            raise
        result = _public_camera(camera)
        result["publish_credentials"] = {
            "username": payload.camera_id,
            "password": publish_password,
        }
        response.headers["Cache-Control"] = "no-store"
        return result


@router.patch("/api/v1/cameras/{camera_id}", response_model=CameraResponse)
async def update_camera(
    request: Request,
    camera_id: str,
    payload: CameraPatch,
    _: Principal = Depends(require_admin),
    settings: Settings = Depends(get_settings_dependency),
    data: DataClient = Depends(get_data_client),
) -> Any:
    if not CAMERA_ID_PATTERN.fullmatch(camera_id):
        raise HTTPException(status_code=400, detail="Invalid camera ID")
    body = payload.model_dump(exclude_unset=True)
    body.pop("edge_auth_token", None)
    if payload.edge_auth_token is not None:
        body["edge_auth_token"] = payload.edge_auth_token.get_secret_value()
    if not body:
        raise HTTPException(status_code=400, detail="No camera fields supplied")
    _validate_source_url(body.get("source_url"))
    _validate_edge_url(body.get("edge_management_url"), "edge_management_url")
    _validate_edge_url(body.get("edge_recovery_url"), "edge_recovery_url")
    if body.get("stream_path") is not None and body["stream_path"] != camera_id:
        raise HTTPException(status_code=400, detail="stream_path must match camera_id")
    if "enabled" in body:
        body["status"] = "offline" if body["enabled"] else "disabled"
    async with camera_lifecycle_lock(request, camera_id):
        updated = await data.update_camera(camera_id, body)
        if "enabled" in body:
            await data.put_camera_runtime_status(
                camera_id,
                {
                    "online": False,
                    "camera_input": "unknown",
                    "central_connection_status": "unknown",
                    "last_error_code": (None if body["enabled"] else "CAMERA_DISABLED"),
                },
            )
            if not body["enabled"]:
                # 먼저 비활성 상태를 저장해 재접속을 차단한다.
                # MediaMTX 연결 끊기에 실패해도 비활성 상태를 유지하므로 안전하게 재시도할 수 있다.
                await _disconnect_camera_publisher(settings, camera_id)
        return _public_camera(updated)


@router.delete("/api/v1/cameras/{camera_id}", status_code=204)
async def delete_camera(
    request: Request,
    camera_id: str,
    principal: Principal = Depends(require_admin),
    settings: Settings = Depends(get_settings_dependency),
    data: DataClient = Depends(get_data_client),
) -> Response:
    if not CAMERA_ID_PATTERN.fullmatch(camera_id):
        raise HTTPException(status_code=400, detail="Invalid camera ID")
    async with camera_lifecycle_lock(request, camera_id):
        deletion_status = await data.get_camera_deletion_status(camera_id)
        if not bool(deletion_status.get("deletable")):
            raise DataConflict(
                "Camera history must be retained; disable the camera instead.",
                code="CAMERA_HAS_HISTORY",
            )
        previous = await data.get_camera(camera_id, user_id=principal.user_id)
        # 새 송출을 막고 기존 연결을 끊은 뒤 삭제한다.
        # 제어 통신이 실패하면 비활성 카메라 기록을 남겨 다음 요청에서 이어서 처리한다.
        await data.update_camera(camera_id, {"enabled": False, "status": "disabled"})
        await data.put_camera_runtime_status(
            camera_id,
            {
                "online": False,
                "camera_input": "unknown",
                "central_connection_status": "unknown",
                "last_error_code": "CAMERA_DISABLED",
            },
        )
        await _disconnect_camera_publisher(settings, camera_id)
        try:
            await data.delete_camera(camera_id)
        except DataConflict:
            # A recording/event can arrive between the preflight check and
            # the transactional delete. Restore admission in that rare race
            # so a history conflict never silently strands a live camera.
            was_enabled = bool(previous.get("enabled", True))
            await data.update_camera(
                camera_id,
                {
                    "enabled": was_enabled,
                    "status": "offline" if was_enabled else "disabled",
                },
            )
            await data.put_camera_runtime_status(
                camera_id,
                {
                    "online": False,
                    "camera_input": "unknown",
                    "central_connection_status": "unknown",
                    "last_error_code": (
                        "DELETE_ABORTED_HISTORY" if was_enabled else "CAMERA_DISABLED"
                    ),
                },
            )
            raise
        return Response(status_code=204)


@router.post(
    "/api/v1/cameras/{camera_id}/publish-credentials/rotate",
    response_model=CameraResponse,
)
async def rotate_camera_publish_credentials(
    request: Request,
    camera_id: str,
    response: Response,
    principal: Principal = Depends(require_admin),
    settings: Settings = Depends(get_settings_dependency),
    data: DataClient = Depends(get_data_client),
) -> Any:
    """Rotate one camera credential and return the plaintext exactly once."""

    if not CAMERA_ID_PATTERN.fullmatch(camera_id):
        raise HTTPException(status_code=400, detail="Invalid camera ID")
    async with camera_lifecycle_lock(request, camera_id):
        camera = await data.get_camera(camera_id, user_id=principal.user_id)
        was_enabled = bool(camera.get("enabled", True))

        # 기존 연결을 끊고 저장된 송출 비밀번호를 교체하는 동안 재접속을 막는다.
        # 중간 실패 시 카메라는 비활성 상태로 남아 이전 비밀번호로 다시 송출하지 못한다.
        await data.update_camera(camera_id, {"enabled": False, "status": "disabled"})
        await data.put_camera_runtime_status(
            camera_id,
            {
                "online": False,
                "camera_input": "unknown",
                "central_connection_status": "unknown",
                "last_error_code": "PUBLISH_CREDENTIAL_ROTATING",
            },
        )
        await _disconnect_camera_publisher(settings, camera_id)

        publish_password = secrets.token_urlsafe(32)
        await data.put_camera_publish_credential(
            camera_id,
            {
                "username": camera_id,
                "password_hash": hash_password(publish_password),
            },
        )
        updated = await data.update_camera(
            camera_id,
            {
                "enabled": was_enabled,
                "status": "offline" if was_enabled else "disabled",
            },
        )
        await data.put_camera_runtime_status(
            camera_id,
            {
                "online": False,
                "camera_input": "unknown",
                "central_connection_status": "unknown",
                "last_error_code": None if was_enabled else "CAMERA_DISABLED",
            },
        )
        result = _public_camera(updated)
        result["publish_credentials"] = {
            "username": camera_id,
            "password": publish_password,
        }
        response.headers["Cache-Control"] = "no-store"
        return result


@router.get("/api/v1/cameras/{camera_id}", response_model=CameraResponse)
async def get_camera(
    camera_id: str,
    principal: Principal = Depends(get_current_principal),
    data: DataClient = Depends(get_data_client),
) -> Any:
    return _public_camera(await _ensure_camera_access(data, principal, camera_id))


@router.get("/api/v1/cameras/{camera_id}/live", response_model=CameraLiveResponse)
async def get_camera_live(
    camera_id: str,
    principal: Principal = Depends(get_current_principal),
    settings: Settings = Depends(get_settings_dependency),
    data: DataClient = Depends(get_data_client),
) -> Any:
    camera = await _ensure_camera_access(data, principal, camera_id)
    if not bool(camera.get("enabled", True)):
        raise HTTPException(status_code=409, detail="Camera is disabled")
    relative_url = (
        f"{settings.public_hls_prefix}/{quote(camera_id, safe='')}/index.m3u8"
    )
    media_url = _public_media_url(settings, relative_url)
    return {
        "camera_id": camera_id,
        "protocol": "hls",
        "url": media_url,
        "hls_url": media_url,
        "auth": {
            "method": "cookie",
            "cookie_name": settings.access_cookie_name,
        },
    }


@router.get(
    "/api/v1/cameras/{camera_id}/video-profile",
    response_model=VideoProfileResponse,
)
async def get_camera_video_profile(
    camera_id: str,
    principal: Principal = Depends(get_current_principal),
    data: DataClient = Depends(get_data_client),
) -> Any:
    await _ensure_camera_access(data, principal, camera_id)
    profile = await data.get_camera_video_profile(camera_id)
    return {
        key: profile.get(key)
        for key in (
            "camera_id",
            "current_profile",
            "desired_profile",
            "supported_profiles",
            "edge_online",
            "last_error_code",
        )
    }


@router.patch(
    "/api/v1/cameras/{camera_id}/video-profile",
    response_model=VideoProfileResponse,
)
async def update_camera_video_profile(
    camera_id: str,
    payload: VideoProfilePatch,
    _: Principal = Depends(require_admin),
    settings: Settings = Depends(get_settings_dependency),
    data: DataClient = Depends(get_data_client),
    _lifecycle_lock: None = Depends(hold_camera_lifecycle_lock),
) -> Any:
    await _ensure_camera_access(data, _, camera_id)
    await data.update_camera_video_profile(
        camera_id, {"desired_profile": payload.profile}
    )
    try:
        target = await data.get_camera_control_target(camera_id)
    except DataConflict as exc:
        edge_error = EdgeControlError(
            "CAPABILITY_UNKNOWN",
            "Edge management metadata is not configured.",
            status_code=409,
        )
        await data.update_camera_video_profile(
            camera_id, {"last_error_code": edge_error.code}
        )
        await data.create_event(
            {
                "camera_id": camera_id,
                "event_type": "video_profile_change_failed",
                "occurred_at": datetime.now(timezone.utc).isoformat(),
                "metadata": {
                    "requested_profile": payload.profile,
                    "reason_code": edge_error.code,
                },
            }
        )
        raise edge_error from exc

    edge = EdgeHttpClient(
        base_url=str(target["management_url"]),
        auth_token=str(target["auth_token"]),
        timeout_seconds=settings.edge_control_timeout_seconds,
    )
    try:
        capabilities = await edge.get_video_capabilities()
        if capabilities.get("camera_id") != camera_id:
            raise EdgeControlError(
                "INVALID_EDGE_RESPONSE",
                "The Edge capability camera ID did not match.",
            )
        capability_error = _edge_capability_error(capabilities)
        if capability_error is not None:
            raise capability_error
        supported = capabilities.get("supported_profiles")
        if (
            not isinstance(supported, list)
            or not supported
            or any(profile not in {"hd", "fhd"} for profile in supported)
        ):
            raise EdgeControlError(
                "INVALID_EDGE_RESPONSE",
                "The Edge device returned invalid video capabilities.",
            )
        await data.update_camera_video_profile(
            camera_id,
            {
                "supported_profiles": supported,
                "encoder": capabilities.get("encoder") or "unknown",
            },
        )
        if payload.profile not in supported:
            raise EdgeControlError(
                "UNSUPPORTED_VIDEO_PROFILE",
                "The Edge device does not support the requested video profile.",
                status_code=409,
                details={
                    "requested_profile": payload.profile,
                    "supported_profiles": supported,
                },
            )
        applied = await edge.apply_video_profile(payload.profile)
        if applied.get("current_profile") != payload.profile:
            raise EdgeControlError(
                "INVALID_EDGE_RESPONSE",
                "The Edge device did not confirm the requested video profile.",
                profile_outcome_journaled=True,
            )
    except EdgeControlError as exc:
        await data.update_camera_video_profile(camera_id, {"last_error_code": exc.code})
        if not exc.profile_outcome_journaled:
            await data.create_event(
                {
                    "camera_id": camera_id,
                    "event_type": "video_profile_change_failed",
                    "occurred_at": datetime.now(timezone.utc).isoformat(),
                    "metadata": {
                        "requested_profile": payload.profile,
                        "reason_code": exc.code,
                    },
                }
            )
        raise
    finally:
        await edge.close()

    # AC-002: current_profile changes only after the Edge's explicit
    # applied response above has been validated.
    profile = await data.update_camera_video_profile(
        camera_id,
        {
            "current_profile": payload.profile,
            "supported_profiles": supported,
            "encoder": capabilities.get("encoder") or "unknown",
            "last_error_code": None,
        },
    )
    # The Data Service mirrors current_profile to camera runtime state in
    # the same transaction. Avoid a partial status write here: a successful
    # control call does not refresh CPU/power/input telemetry.
    # ProfileManager wrote the authoritative success event before it sent
    # the applied response. The Status Collector imports that durable Edge
    # journal entry; writing another event here would duplicate it.
    return {
        key: profile.get(key)
        for key in (
            "camera_id",
            "current_profile",
            "desired_profile",
            "supported_profiles",
            "edge_online",
            "last_error_code",
        )
    }


@router.get(
    "/api/v1/cameras/{camera_id}/status",
    response_model=CameraStatusResponse,
)
async def get_camera_status(
    camera_id: str,
    principal: Principal = Depends(get_current_principal),
    data: DataClient = Depends(get_data_client),
) -> Any:
    await _ensure_camera_access(data, principal, camera_id)
    runtime = await data.get_camera_runtime_status(camera_id)
    allowed = (
        "camera_id",
        "online",
        "cpu_percent",
        "memory_percent",
        "storage_percent",
        "battery_percent",
        "power_source",
        "camera_input",
        "central_connection_status",
        "current_video_profile",
        "last_seen_at",
        "last_error_code",
    )
    return {key: runtime.get(key) for key in allowed}
