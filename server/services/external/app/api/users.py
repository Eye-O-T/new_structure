# 관리자만 사용자 계정과 카메라별 열람 권한을 변경하도록 하는 공개 API이다.
from __future__ import annotations

from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
)

from ..clients.data import (
    DataClient,
)
from ..dependencies import (
    get_data_client,
)
from ..schemas import (
    CameraPermissionListResponse,
    CameraPermissions,
    UserCreate,
    UserPageResponse,
    UserPatch,
    UserResponse,
)
from ..security.passwords import hash_password
from ..security.permissions import Principal, require_admin
from .representations import _public_user, _public_users
from .validation import _validate_resource_id

router = APIRouter()


@router.get("/api/v1/admin/users", response_model=UserPageResponse)
async def list_users(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    _: Principal = Depends(require_admin),
    data: DataClient = Depends(get_data_client),
) -> Any:
    return _public_users(await data.list_users(limit=limit, offset=offset))


@router.post("/api/v1/admin/users", status_code=201, response_model=UserResponse)
async def create_user(
    payload: UserCreate,
    _: Principal = Depends(require_admin),
    data: DataClient = Depends(get_data_client),
) -> Any:
    body = {
        "username": payload.username,
        "password_hash": hash_password(payload.password.get_secret_value()),
        "role": payload.role,
        "is_active": payload.is_active,
    }
    return _public_user(await data.create_user(body))


@router.patch("/api/v1/admin/users/{user_id}", response_model=UserResponse)
async def update_user(
    user_id: str,
    payload: UserPatch,
    _: Principal = Depends(require_admin),
    data: DataClient = Depends(get_data_client),
) -> Any:
    user_id = _validate_resource_id(user_id)
    body = payload.model_dump(exclude_unset=True, exclude={"password"})
    if payload.password is not None:
        body["password_hash"] = hash_password(payload.password.get_secret_value())
    if not body:
        raise HTTPException(status_code=400, detail="No user fields supplied")
    return _public_user(await data.update_user(user_id, body))


@router.get(
    "/api/v1/admin/users/{user_id}/camera-permissions",
    response_model=CameraPermissionListResponse,
)
async def get_user_permissions(
    user_id: str,
    _: Principal = Depends(require_admin),
    data: DataClient = Depends(get_data_client),
) -> Any:
    return await data.get_camera_permissions(_validate_resource_id(user_id))


@router.put(
    "/api/v1/admin/users/{user_id}/camera-permissions",
    response_model=CameraPermissionListResponse,
)
async def set_user_permissions(
    user_id: str,
    payload: CameraPermissions,
    _: Principal = Depends(require_admin),
    data: DataClient = Depends(get_data_client),
) -> Any:
    try:
        camera_ids = payload.normalized_camera_ids()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid camera ID") from exc
    return await data.set_camera_permissions(
        _validate_resource_id(user_id),
        camera_ids,
    )
