# 사용자 계정과 카메라별 열람 권한을 저장하는 내부 API이다.
"""Internal users API."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Query,
    Response,
    status,
)

from ai_cctv_core.identifiers import validate_camera_id

from ..dependencies import Repo, _page
from ..errors import ApiError, _not_found
from ..schemas import (
    CameraPermissionsReplace,
    UserCreate,
    UserUpdate,
)

router = APIRouter()


@router.post("/users", status_code=status.HTTP_201_CREATED)
def create_user(payload: UserCreate, repository: Repo) -> dict[str, Any]:
    return repository.create_user(payload.model_dump(mode="json"))


@router.get("/users/by-username/{username}")
def get_user_by_username(username: str, repository: Repo) -> dict[str, Any]:
    user = repository.get_user_by_username(username)
    if user is None:
        raise _not_found("user")
    return user


@router.get("/users")
def list_users(
    repository: Repo,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    return _page(repository.list_users(limit, offset), limit, offset)


@router.get("/users/{user_id}")
def get_user(user_id: int, repository: Repo) -> dict[str, Any]:
    user = repository.get_user(user_id)
    if user is None:
        raise _not_found("user")
    return user


@router.patch("/users/{user_id}")
def update_user(user_id: int, payload: UserUpdate, repository: Repo) -> dict[str, Any]:
    values = payload.model_dump(mode="json", exclude_unset=True)
    if any(value is None for value in values.values()):
        raise ApiError(422, "VALIDATION_ERROR", "사용자 필드는 null일 수 없습니다.")
    user = repository.update_user(user_id, values)
    if user is None:
        raise _not_found("user")
    return user


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(user_id: int, repository: Repo) -> Response:
    if not repository.delete_user(user_id):
        raise _not_found("user")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.put("/users/{user_id}/camera-permissions/{camera_id}")
def grant_camera_permission(
    user_id: int, camera_id: str, repository: Repo
) -> dict[str, Any]:
    validate_camera_id(camera_id)
    if repository.get_user(user_id) is None:
        raise _not_found("user")
    result = repository.grant_camera(user_id, camera_id)
    if result is None:
        raise _not_found("camera")
    return result


@router.delete(
    "/users/{user_id}/camera-permissions/{camera_id}",
    status_code=status.HTTP_204_NO_CONTENT,
)
def revoke_camera_permission(
    user_id: int, camera_id: str, repository: Repo
) -> Response:
    if not repository.revoke_camera(user_id, camera_id):
        raise _not_found("camera_permission")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/users/{user_id}/camera-permissions")
def list_camera_permissions(user_id: int, repository: Repo) -> dict[str, Any]:
    if repository.get_user(user_id) is None:
        raise _not_found("user")
    return {"items": repository.list_user_cameras(user_id)}


@router.put("/users/{user_id}/camera-permissions")
def replace_camera_permissions(
    user_id: int,
    payload: CameraPermissionsReplace,
    repository: Repo,
) -> dict[str, Any]:
    try:
        items = repository.replace_camera_permissions(user_id, payload.camera_ids)
    except LookupError as exc:
        raise _not_found("user") from exc
    except ValueError as exc:
        raise ApiError(
            404,
            "CAMERA_NOT_FOUND",
            "One or more requested cameras were not found.",
        ) from exc
    return {"items": items}
