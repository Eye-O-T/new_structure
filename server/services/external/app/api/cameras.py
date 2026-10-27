# 카메라 설정과 영상·녹화 제어를 처리하며 Data·Edge·MediaMTX 작업 순서를 조정한다.
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from typing import Any, Callable
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
from ..clients.mediamtx import MediaControlError, MediaMtxClient
from ..config import CAMERA_ID_PATTERN, Settings
from ..dependencies import (
    camera_admission_lock,
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
    CameraRecordingBulkItemResponse,
    CameraRecordingBulkResponse,
    CameraRecordingListResponse,
    CameraRecordingResponse,
    CameraResponse,
    CameraStatusResponse,
    RecordingState,
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


def _new_media_client(
    settings: Settings,
    request: Request | None = None,
    *,
    media_client_factory: Callable[[], MediaMtxClient] | None = None,
) -> MediaMtxClient:
    if media_client_factory is not None:
        return media_client_factory()
    if request is not None:
        state_factory = getattr(request.app.state, "media_client_factory", None)
        if callable(state_factory):
            return state_factory()
    return MediaMtxClient(
        settings.media_control_url,
        timeout_seconds=settings.media_control_timeout_seconds,
    )


# 송출 해제 성공 여부와 관계없이 일회성 MediaMTX 제어 연결을 정리한다.
async def _disconnect_camera_publisher(
    settings: Settings, camera_id: str, request: Request | None = None
) -> bool:
    client = _new_media_client(settings, request)
    try:
        return await client.disconnect_publisher(camera_id)
    finally:
        await client.close()


async def _sync_camera_path_recording_best_effort(
    settings: Settings,
    camera_id: str,
    record: bool,
    request: Request | None = None,
) -> None:
    client = _new_media_client(settings, request)
    try:
        await client.set_path_recording(camera_id, record)
    except MediaControlError:
        pass
    finally:
        await client.close()


async def _delete_camera_path_config_best_effort(
    settings: Settings,
    camera_id: str,
    request: Request | None = None,
) -> None:
    client = _new_media_client(settings, request)
    try:
        await client.delete_path_config(camera_id)
    except MediaControlError:
        pass
    finally:
        await client.close()


def _compute_recording_status(
    camera: dict[str, Any],
    path_status: dict[str, Any] | None = None,
    *,
    media_error_code: str | None = None,
) -> dict[str, Any]:
    enabled = bool(camera.get("enabled", True))
    recording_enabled = bool(camera.get("recording_enabled", False))
    error_code = media_error_code or camera.get("recording_last_error_code")
    stream_ready = bool(path_status and path_status.get("ready"))
    file_active = bool(camera.get("recording_file_active"))

    state: RecordingState
    if error_code:
        state = "error"
    elif not enabled or not recording_enabled:
        state = "stopped"
    elif stream_ready or (path_status is None and file_active):
        state = "recording"
    else:
        state = "waiting"

    return {
        "camera_id": str(camera["camera_id"]),
        "enabled": enabled,
        "recording_enabled": recording_enabled,
        "recording_state": state,
        "stream_ready": stream_ready,
        "recording_file_active": file_active,
        "last_error_code": error_code,
        "updated_at": camera.get("updated_at"),
    }


async def sync_camera_recording_paths(
    settings: Settings,
    data: DataClient,
    *,
    media_client_factory: Callable[[], MediaMtxClient] | None = None,
) -> dict[str, int]:
    """DB에 저장된 카메라별 녹화 희망 상태를 MediaMTX 경로 설정과 동기화한다."""

    page = await data.list_cameras(limit=200, offset=0)
    items = page.get("items", []) if isinstance(page, dict) else []
    cameras_list = [item for item in items if isinstance(item, dict)]
    if not cameras_list:
        return {"total": 0, "synced": 0, "failed": 0}

    client = _new_media_client(settings, media_client_factory=media_client_factory)
    synced = 0
    failed = 0
    try:
        for camera in cameras_list:
            camera_id = str(camera["camera_id"])
            should_record = bool(camera.get("enabled", True)) and bool(
                camera.get("recording_enabled", False)
            )
            try:
                await client.set_path_recording(camera_id, should_record)
                if camera.get("recording_last_error_code") is not None:
                    await data.update_camera(
                        camera_id, {"recording_last_error_code": None}
                    )
                synced += 1
            except MediaControlError as exc:
                failed += 1
                if camera.get("recording_last_error_code") != exc.code:
                    try:
                        await data.update_camera(
                            camera_id, {"recording_last_error_code": exc.code}
                        )
                    except DataServiceError:
                        pass
    finally:
        await client.close()
    return {"total": len(cameras_list), "synced": synced, "failed": failed}


async def _apply_camera_recording(
    request: Request,
    settings: Settings,
    data: DataClient,
    camera_id: str,
    desired_recording: bool,
) -> dict[str, Any]:
    if not CAMERA_ID_PATTERN.fullmatch(camera_id):
        raise HTTPException(status_code=400, detail="Invalid camera ID")
    async with camera_lifecycle_lock(request, camera_id):
        camera = await data.get_camera(camera_id)
        if desired_recording and not bool(camera.get("enabled", True)):
            raise DataConflict(
                "Disabled cameras cannot start recording.",
                code="CAMERA_DISABLED",
            )
        camera = await data.update_camera(
            camera_id,
            {
                "recording_enabled": bool(desired_recording),
                "recording_last_error_code": None,
            },
        )
        should_record = bool(camera.get("enabled", True)) and bool(desired_recording)
        client = _new_media_client(settings, request)
        try:
            await client.set_path_recording(camera_id, should_record)
            path_status = await client.get_path_status(camera_id)
        except MediaControlError as exc:
            try:
                await data.update_camera(
                    camera_id, {"recording_last_error_code": exc.code}
                )
            except DataServiceError:
                pass
            raise
        finally:
            await client.close()
        refreshed = await data.get_camera(camera_id)
        return _compute_recording_status(refreshed, path_status)


async def _apply_bulk_camera_recording(
    request: Request,
    settings: Settings,
    data: DataClient,
    _principal: Principal,
    desired_recording: bool,
) -> dict[str, Any]:
    page = await data.list_cameras(limit=200, offset=0)
    raw_items = page.get("items", []) if isinstance(page, dict) else []
    active_cameras = [
        item
        for item in raw_items
        if isinstance(item, dict) and bool(item.get("enabled", True))
    ]
    results: list[dict[str, Any]] = []
    succeeded = 0
    failed = 0
    for camera in active_cameras:
        camera_id = str(camera["camera_id"])
        try:
            status_payload = await _apply_camera_recording(
                request, settings, data, camera_id, desired_recording
            )
            succeeded += 1
            results.append(
                {
                    "camera_id": camera_id,
                    "ok": True,
                    "enabled": status_payload["enabled"],
                    "recording_enabled": status_payload["recording_enabled"],
                    "recording_state": status_payload["recording_state"],
                    "stream_ready": status_payload["stream_ready"],
                    "recording_file_active": status_payload["recording_file_active"],
                    "error_code": None,
                    "message": None,
                }
            )
        except (MediaControlError, DataServiceError, HTTPException) as exc:
            failed += 1
            if isinstance(exc, MediaControlError):
                error_code = exc.code
                message = exc.message
            elif isinstance(exc, DataServiceError):
                error_code = exc.code
                message = exc.message
            else:
                error_code = "RECORDING_CONTROL_FAILED"
                message = str(exc.detail)
            results.append(
                {
                    "camera_id": camera_id,
                    "ok": False,
                    "enabled": bool(camera.get("enabled", True)),
                    "recording_enabled": bool(desired_recording),
                    "recording_state": "error",
                    "stream_ready": False,
                    "recording_file_active": bool(camera.get("recording_file_active")),
                    "error_code": error_code,
                    "message": message,
                }
            )
    return {
        "items": results,
        "total": len(results),
        "succeeded": succeeded,
        "failed": failed,
    }


# 내부 RTSP·Edge 주소 및 인증값을 제거하고 실행 상태는 별도 상태 API에 맡긴다.
def _public_camera(
    camera: dict[str, Any],
    path_status: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = dict(camera)
    if "recording_enabled" in result and "recording_state" not in result:
        computed = _compute_recording_status(result, path_status)
        result["recording_state"] = computed["recording_state"]
    for internal_field in (
        "source_url",
        "edge_device_id",
        "edge_management_url",
        "edge_recovery_url",
        "edge_auth_token",
        "management_url",
        "recovery_url",
        "auth_token",
        "recording_file_active",
        # cameras.status는 구형 스키마 호환 필드다. 실행 상태는 /cameras/{id}/status에서만 제공한다.
        "status",
    ):
        result.pop(internal_field, None)
    return result


# 목록·페이지·단일 카메라 응답 모두 같은 비공개 필드 제거 절차를 적용한다.
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


# 카메라 입력은 호스트가 있는 RTSP(S) 주소만 허용하고 제어 문자를 거절한다.
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


# Edge 접속 주소에서 사용자 정보·질의·프래그먼트·상위 경로를 차단한다.
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


# 지원 프로필 목록보다 입력·인코더의 사용 가능 여부를 먼저 판정하여 정확한 실패 원인을 알린다.
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


# 현재 사용자의 ID로 권한 필터를 적용하고 내부 필드를 제거한 페이지를 반환한다.
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


# 비활성 등록·송출 해시 저장·활성화를 같은 카메라 잠금 안에서 순서대로 수행한다.
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
    # DB 인증값을 저장할 때까지 송출을 막아 재등록 중 옛 정적 인증값이 허용되지 않게 한다.
    create_body = {
        **body,
        "enabled": False,
        "recording_enabled": False,
        "status": "disabled",
    }
    async with (
        camera_lifecycle_lock(request, payload.camera_id),
        camera_admission_lock(request, payload.camera_id),
    ):
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
            # 기존 송출을 끊기 전에 행을 지우면 옛 정적 인증값이 다시 허용될 수 있다.
            # MediaMTX 연결 실패 시에도 비활성 행을 남겨 관리자가 재시도하게 한다.
            await _disconnect_camera_publisher(settings, payload.camera_id, request)
            await data.delete_camera(payload.camera_id)
            raise
        await _sync_camera_path_recording_best_effort(
            settings,
            payload.camera_id,
            bool(camera.get("enabled", True))
            and bool(camera.get("recording_enabled", False)),
            request,
        )
        result = _public_camera(camera)
        result["publish_credentials"] = {
            "username": payload.camera_id,
            "password": publish_password,
        }
        response.headers["Cache-Control"] = "no-store"
        return result


@router.get(
    "/api/v1/cameras/recording",
    response_model=CameraRecordingListResponse,
)
async def list_cameras_recording(
    request: Request,
    principal: Principal = Depends(get_current_principal),
    settings: Settings = Depends(get_settings_dependency),
    data: DataClient = Depends(get_data_client),
) -> Any:
    page = await data.list_cameras(user_id=principal.user_id, limit=200, offset=0)
    raw_items = page.get("items", []) if isinstance(page, dict) else []
    cameras_list = [item for item in raw_items if isinstance(item, dict)]

    paths: dict[str, dict[str, Any]] = {}
    media_error_code: str | None = None
    if cameras_list:
        client = _new_media_client(settings, request)
        try:
            paths = await client.list_paths()
        except MediaControlError as exc:
            media_error_code = exc.code
        finally:
            await client.close()

    items = [
        _compute_recording_status(
            camera,
            paths.get(str(camera["camera_id"])),
            media_error_code=media_error_code,
        )
        for camera in cameras_list
    ]
    return {"items": items}


@router.post(
    "/api/v1/cameras/recording/start",
    response_model=CameraRecordingBulkResponse,
)
async def start_all_cameras_recording(
    request: Request,
    principal: Principal = Depends(require_admin),
    settings: Settings = Depends(get_settings_dependency),
    data: DataClient = Depends(get_data_client),
) -> Any:
    return await _apply_bulk_camera_recording(
        request, settings, data, principal, True
    )


@router.post(
    "/api/v1/cameras/recording/stop",
    response_model=CameraRecordingBulkResponse,
)
async def stop_all_cameras_recording(
    request: Request,
    principal: Principal = Depends(require_admin),
    settings: Settings = Depends(get_settings_dependency),
    data: DataClient = Depends(get_data_client),
) -> Any:
    return await _apply_bulk_camera_recording(
        request, settings, data, principal, False
    )


# 부분 설정을 검증하고 활성 상태 변경 시 오래된 실행 상태와 송출 연결도 정리한다.
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
    async with (
        camera_lifecycle_lock(request, camera_id),
        camera_admission_lock(request, camera_id),
    ):
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
            await _sync_camera_path_recording_best_effort(
                settings,
                camera_id,
                bool(updated.get("enabled", True))
                and bool(updated.get("recording_enabled", False)),
                request,
            )
            if not body["enabled"]:
                # 먼저 비활성 상태를 저장해 재접속을 차단한다.
                # MediaMTX 연결 끊기에 실패해도 비활성 상태를 유지하므로 안전하게 재시도할 수 있다.
                await _disconnect_camera_publisher(settings, camera_id, request)
        return _public_camera(updated)


# 이력 사전 검사 후 신규 송출 차단·기존 연결 해제·DB 삭제 순서로 처리한다.
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
    async with (
        camera_lifecycle_lock(request, camera_id),
        camera_admission_lock(request, camera_id),
    ):
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
        await _disconnect_camera_publisher(settings, camera_id, request)
        try:
            await data.delete_camera(camera_id)
        except DataConflict:
            # 사전 검사 뒤 녹화·이벤트가 들어와 삭제가 거절되면 송출 허용을 복구해 카메라가 멈추지 않게 한다.
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
        await _delete_camera_path_config_best_effort(settings, camera_id, request)
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
    """카메라 인증값을 교체하고 원문은 이 응답에서만 반환한다."""

    if not CAMERA_ID_PATTERN.fullmatch(camera_id):
        raise HTTPException(status_code=400, detail="Invalid camera ID")
    async with (
        camera_lifecycle_lock(request, camera_id),
        camera_admission_lock(request, camera_id),
    ):
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
        await _disconnect_camera_publisher(settings, camera_id, request)

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


# 대상 카메라의 현재 권한을 확인한 결과에서 공개 필드만 반환한다.
@router.get("/api/v1/cameras/{camera_id}", response_model=CameraResponse)
async def get_camera(
    camera_id: str,
    principal: Principal = Depends(get_current_principal),
    data: DataClient = Depends(get_data_client),
) -> Any:
    return _public_camera(await _ensure_camera_access(data, principal, camera_id))


@router.get(
    "/api/v1/cameras/{camera_id}/recording",
    response_model=CameraRecordingResponse,
)
async def get_camera_recording(
    request: Request,
    camera_id: str,
    principal: Principal = Depends(get_current_principal),
    settings: Settings = Depends(get_settings_dependency),
    data: DataClient = Depends(get_data_client),
) -> Any:
    camera = await _ensure_camera_access(data, principal, camera_id)
    path_status: dict[str, Any] | None = None
    media_error_code: str | None = None
    client = _new_media_client(settings, request)
    try:
        path_status = await client.get_path_status(camera_id)
    except MediaControlError as exc:
        media_error_code = exc.code
    finally:
        await client.close()
    return _compute_recording_status(
        camera, path_status, media_error_code=media_error_code
    )


@router.post(
    "/api/v1/cameras/{camera_id}/recording/start",
    response_model=CameraRecordingResponse,
)
async def start_camera_recording(
    request: Request,
    camera_id: str,
    _: Principal = Depends(require_admin),
    settings: Settings = Depends(get_settings_dependency),
    data: DataClient = Depends(get_data_client),
) -> Any:
    return await _apply_camera_recording(
        request, settings, data, camera_id, True
    )


@router.post(
    "/api/v1/cameras/{camera_id}/recording/stop",
    response_model=CameraRecordingResponse,
)
async def stop_camera_recording(
    request: Request,
    camera_id: str,
    _: Principal = Depends(require_admin),
    settings: Settings = Depends(get_settings_dependency),
    data: DataClient = Depends(get_data_client),
) -> Any:
    return await _apply_camera_recording(
        request, settings, data, camera_id, False
    )


# 권한이 있고 활성인 카메라에 대해 쿠키 인증이 필요한 HLS 재생 계약을 만든다.
@router.get("/api/v1/cameras/{camera_id}/live", response_model=CameraLiveResponse)
async def get_camera_live(
    camera_id: str,
    protocol: str = Query("hls", pattern="^(hls|rtsp)$"),
    principal: Principal = Depends(get_current_principal),
    settings: Settings = Depends(get_settings_dependency),
    data: DataClient = Depends(get_data_client),
) -> Any:
    camera = await _ensure_camera_access(data, principal, camera_id)
    if not bool(camera.get("enabled", True)):
        raise HTTPException(status_code=409, detail="Camera is disabled")
    if protocol == "rtsp":
        # The read credential is scoped to MediaMTX and is returned only after
        # the caller has passed the normal camera permission check.
        username = quote(settings.media_read_username, safe="")
        password = quote(settings.media_read_password, safe="")
        media_url = (
            f"rtsp://{username}:{password}@{settings.rtsp_public_host}:"
            f"{settings.rtsp_public_port}/{quote(camera_id, safe='')}"
        )
        return {
            "camera_id": camera_id,
            "protocol": "rtsp",
            "url": media_url,
            "auth": {"method": "basic", "username": settings.media_read_username},
        }
    relative_url = f"{settings.public_hls_prefix}/{quote(camera_id, safe='')}/index.m3u8"
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


# 권한 확인 후 사용자가 판단에 필요한 프로필·지원 목록·연결 상태만 노출한다.
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


# 원하는 프로필을 먼저 기록하고 Edge 기능 확인·적용 응답 검증 뒤 실제 프로필을 확정한다.
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

    # Edge의 applied 응답을 검증한 뒤에만 current_profile을 바꾼다.
    profile = await data.update_camera_video_profile(
        camera_id,
        {
            "current_profile": payload.profile,
            "supported_profiles": supported,
            "encoder": capabilities.get("encoder") or "unknown",
            "last_error_code": None,
        },
    )
    # Data가 같은 트랜잭션에서 실행 프로필도 갱신한다. 제어 응답만으로 CPU·전원·입력 상태를 바꾸지 않는다.
    # 성공 이벤트는 Edge 일지에서 수집하므로 여기서 중복 기록하지 않는다.
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


# Data의 내부 비교용 상태는 제외하고 카메라 권한을 확인한 공개 상태 필드만 반환한다.
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
