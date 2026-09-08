# 사용자가 접근할 수 있는 카메라의 이벤트를 조회하도록 권한을 확인한다.
from __future__ import annotations

from datetime import datetime
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
from ..config import CAMERA_ID_PATTERN
from ..dependencies import (
    get_data_client,
)
from ..schemas import (
    EventPageResponse,
    EventResponse,
)
from ..security.permissions import (
    Principal,
    _ensure_camera_access,
    get_current_principal,
)
from .validation import _validate_resource_id, _validated_time_range

router = APIRouter()


@router.get("/api/v1/events", response_model=EventPageResponse)
async def list_events(
    camera_id: str | None = Query(default=None),
    event_type: str | None = Query(default=None, min_length=1, max_length=128),
    start: datetime | None = Query(default=None, alias="from"),
    end: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_current_principal),
    data: DataClient = Depends(get_data_client),
) -> Any:
    if camera_id is not None and not CAMERA_ID_PATTERN.fullmatch(camera_id):
        raise HTTPException(status_code=400, detail="Invalid camera ID")
    if principal.role != "admin" and camera_id is None:
        raise HTTPException(
            status_code=400,
            detail="camera_id is required for viewer event searches",
        )
    if camera_id is not None:
        await _ensure_camera_access(data, principal, camera_id)
    start_utc, end_utc = _validated_time_range(start, end)
    return await data.list_events(
        user_id=principal.user_id,
        camera_id=camera_id,
        event_type=event_type,
        start=start_utc,
        end=end_utc,
        limit=limit,
        offset=offset,
    )


@router.get("/api/v1/events/{event_id}", response_model=EventResponse)
async def get_event(
    event_id: str,
    principal: Principal = Depends(get_current_principal),
    data: DataClient = Depends(get_data_client),
) -> Any:
    event = await data.get_event(
        _validate_resource_id(event_id),
        user_id=principal.user_id,
    )
    await _ensure_camera_access(data, principal, str(event.get("camera_id", "")))
    return event
