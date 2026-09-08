# 카메라 등록 정보와 Edge 연결 상태를 읽고 변경하는 내부 API이다.

from __future__ import annotations

from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Query,
    Response,
    status,
)

from ai_cctv_core.identifiers import validate_camera_id
from ai_cctv_core.time import format_utc

from ..database.repositories import CameraHasHistory, CameraLimitReached
from ..dependencies import Repo, _page
from ..errors import ApiError, _not_found
from ..schemas import (
    CameraCreate,
    CameraPublishCredentialPut,
    CameraRuntimeStatusPut,
    CameraStatusUpdate,
    CameraUpdate,
    EdgeDevicePut,
    VideoProfileStatePatch,
)

router = APIRouter()


@router.post("/cameras", status_code=status.HTTP_201_CREATED)
def create_camera(payload: CameraCreate, repository: Repo) -> dict[str, Any]:
    try:
        return repository.create_camera(payload.model_dump(mode="json"))
    except CameraLimitReached as exc:
        raise ApiError(
            409,
            "CAMERA_LIMIT_REACHED",
            "스키마 버전 1은 카메라를 최대 4대까지 지원합니다.",
        ) from exc


@router.put("/edge-devices/{edge_device_id}")
def put_edge_device(
    edge_device_id: str, payload: EdgeDevicePut, repository: Repo
) -> dict[str, Any]:
    return repository.put_edge_device(
        edge_device_id,
        payload.management_url,
        payload.recovery_url,
        payload.auth_token,
    )


@router.get("/edge-devices/{edge_device_id}")
def get_edge_device(edge_device_id: str, repository: Repo) -> dict[str, Any]:
    device = repository.get_edge_device(edge_device_id)
    if device is None:
        raise _not_found("edge_device")
    return device


@router.get("/camera-control-targets")
def list_camera_control_targets(repository: Repo) -> dict[str, Any]:
    return {"items": repository.list_camera_control_targets()}


@router.get("/cameras/enabled")
def enabled_cameras(
    repository: Repo,
    user_id: Annotated[int | None, Query(ge=1)] = None,
) -> dict[str, Any]:
    if user_id is not None and repository.get_user(user_id) is None:
        raise _not_found("user")
    items = repository.list_cameras(200, 0, enabled_only=True, user_id=user_id)
    return {"items": items}


@router.get("/cameras/{camera_id}/deletion-status")
def get_camera_deletion_status(camera_id: str, repository: Repo) -> dict[str, Any]:
    status_result = repository.get_camera_deletion_status(camera_id)
    if status_result is None:
        raise _not_found("camera")
    return status_result


@router.get("/cameras")
def list_cameras(
    repository: Repo,
    user_id: Annotated[int | None, Query(ge=1)] = None,
    enabled_only: bool = False,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    if user_id is not None and repository.get_user(user_id) is None:
        raise _not_found("user")
    return _page(
        repository.list_cameras(
            limit, offset, enabled_only=enabled_only, user_id=user_id
        ),
        limit,
        offset,
    )


@router.get("/cameras/{camera_id}")
def get_camera(camera_id: str, repository: Repo) -> dict[str, Any]:
    camera = repository.get_camera(camera_id)
    if camera is None:
        raise _not_found("camera")
    return camera


@router.patch("/cameras/{camera_id}")
def update_camera(
    camera_id: str, payload: CameraUpdate, repository: Repo
) -> dict[str, Any]:
    values = payload.model_dump(mode="json", exclude_unset=True)
    try:
        camera = repository.update_camera(camera_id, values)
    except CameraLimitReached as exc:
        raise ApiError(
            409,
            "CAMERA_LIMIT_REACHED",
            "At most four cameras can be enabled at once.",
        ) from exc
    if camera is None:
        raise _not_found("camera")
    return camera


@router.patch("/cameras/{camera_id}/status")
def update_camera_status(
    camera_id: str, payload: CameraStatusUpdate, repository: Repo
) -> dict[str, Any]:
    camera = repository.update_camera(camera_id, {"status": payload.status.value})
    if camera is None:
        raise _not_found("camera")
    return camera


@router.get("/cameras/{camera_id}/control-target")
def get_camera_control_target(camera_id: str, repository: Repo) -> dict[str, Any]:
    target = repository.get_camera_control_target(camera_id)
    if target is None:
        if repository.get_camera(camera_id) is None:
            raise _not_found("camera")
        raise ApiError(
            409,
            "CAPABILITY_UNKNOWN",
            "Edge management metadata is not configured.",
        )
    return target


@router.get("/cameras/{camera_id}/video-profile")
def get_camera_video_profile(camera_id: str, repository: Repo) -> dict[str, Any]:
    profile = repository.get_camera_video_profile(camera_id)
    if profile is None:
        raise _not_found("camera")
    return profile


@router.patch("/cameras/{camera_id}/video-profile")
def update_camera_video_profile(
    camera_id: str,
    payload: VideoProfileStatePatch,
    repository: Repo,
) -> dict[str, Any]:
    profile = repository.update_camera_video_profile(
        camera_id, payload.model_dump(mode="json", exclude_unset=True)
    )
    if profile is None:
        raise _not_found("camera")
    return profile


@router.get("/cameras/{camera_id}/runtime-status")
def get_camera_runtime_status(camera_id: str, repository: Repo) -> dict[str, Any]:
    runtime = repository.get_camera_runtime_status(camera_id)
    if runtime is None:
        raise _not_found("camera")
    return runtime


@router.put("/cameras/{camera_id}/runtime-status")
def put_camera_runtime_status(
    camera_id: str,
    payload: CameraRuntimeStatusPut,
    repository: Repo,
) -> dict[str, Any]:
    values = payload.model_dump(mode="json", exclude_unset=True)
    if payload.last_seen_at is not None:
        values["last_seen_at"] = format_utc(payload.last_seen_at)
    runtime = repository.update_camera_runtime_status(camera_id, values)
    if runtime is None:
        raise _not_found("camera")
    return runtime


@router.delete("/cameras/{camera_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_camera(camera_id: str, repository: Repo) -> Response:
    try:
        deleted = repository.delete_camera(camera_id)
    except CameraHasHistory as exc:
        raise ApiError(
            409,
            "CAMERA_HAS_HISTORY",
            "Camera history must be retained; disable the camera instead.",
        ) from exc
    if not deleted:
        raise _not_found("camera")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/cameras/{camera_id}/publish-credential")
def put_camera_publish_credential(
    camera_id: str,
    payload: CameraPublishCredentialPut,
    repository: Repo,
) -> dict[str, Any]:
    validate_camera_id(camera_id)
    credential = repository.put_camera_publish_credential(
        camera_id, payload.username, payload.password_hash
    )
    if credential is None:
        raise _not_found("camera")
    return credential


@router.get("/cameras/{camera_id}/publish-credential")
def get_camera_publish_credential(camera_id: str, repository: Repo) -> dict[str, Any]:
    validate_camera_id(camera_id)
    credential = repository.get_camera_publish_credential(camera_id)
    if credential is None:
        raise _not_found("camera_publish_credential")
    return credential
