# 카메라 권한을 확인한 뒤 모바일의 박스·인물 ID 표시에 필요한 최신 객체 정보를 반환한다.
from __future__ import annotations

from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    Response,
)

from ..clients.data import (
    DataClient,
)
from ..dependencies import (
    get_data_client,
)
from ..security.permissions import (
    Principal,
    _ensure_camera_access,
    get_current_principal,
)

router = APIRouter()


@router.get("/api/v1/cameras/{camera_id}/objects")
async def get_live_objects(
    camera_id: str,
    response: Response,
    principal: Principal = Depends(get_current_principal),
    data: DataClient = Depends(get_data_client),
) -> Any:
    await _ensure_camera_access(data, principal, camera_id)
    response.headers["Cache-Control"] = "no-store"
    return await data.get_live_objects(camera_id)
