# 사용자가 접근할 수 있는 카메라의 이벤트를 조회하도록 권한을 확인한다.
from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
)
from fastapi.responses import StreamingResponse

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


def _public_event(event: dict[str, Any]) -> dict[str, Any]:
    result = dict(event)
    result.pop("snapshot_path", None)
    raw_metadata = result.get("metadata")
    metadata = dict(raw_metadata) if isinstance(raw_metadata, dict) else {}
    raw_object = metadata.get("object")
    obj = dict(raw_object) if isinstance(raw_object, dict) else {}
    has_crop = bool(obj.pop("crop_path", None))
    has_annotated = bool(obj.pop("annotated_snapshot_path", None))
    if "object" in metadata:
        metadata["object"] = obj
    result["metadata"] = metadata
    result["media"] = {
        "snapshot": bool(event.get("snapshot_path")),
        "crop": has_crop,
        "annotated_snapshot": has_annotated,
    }
    return result


# 일반 사용자는 카메라를 지정하고 그 권한을 확인해야 하며 관리자만 전체 검색을 할 수 있다.
@router.get("/api/v1/events", response_model=EventPageResponse)
async def list_events(
    camera_id: str | None = Query(default=None),
    event_type: str | None = Query(default=None, min_length=1, max_length=128),
    start: datetime | None = Query(default=None, alias="from"),
    end: datetime | None = Query(default=None, alias="to"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    cursor: str | None = Query(default=None, min_length=1, max_length=1024),
    order: Literal["asc", "desc"] = Query(default="asc"),
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
    result = await data.list_events(
        user_id=principal.user_id,
        camera_id=camera_id,
        event_type=event_type,
        start=start_utc,
        end=end_utc,
        limit=limit,
        offset=offset,
        cursor=cursor,
        order=order,
    )
    result["items"] = [_public_event(item) for item in result.get("items", [])]
    return result


# 이벤트를 찾은 뒤 소속 카메라 권한을 검사하여 이벤트 ID만 아는 경우의 열람을 막는다.
async def _event_image(event_id: str, kind: str, principal: Principal, data: DataClient) -> StreamingResponse:
    event = await data.get_event(_validate_resource_id(event_id), user_id=principal.user_id)
    await _ensure_camera_access(data, principal, str(event.get("camera_id", "")))
    upstream = await data.open_event_image(event_id, kind)

    async def body():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(
        body(),
        media_type=upstream.headers.get("content-type", "application/octet-stream"),
        headers={"Cache-Control": "private, no-store"},
    )
@router.get("/api/v1/events/{event_id}/snapshot")
async def get_event_snapshot(event_id: str, principal: Principal = Depends(get_current_principal), data: DataClient = Depends(get_data_client)) -> StreamingResponse:
    return await _event_image(event_id, "snapshot", principal, data)


@router.get("/api/v1/events/{event_id}/crop")
async def get_event_crop(event_id: str, principal: Principal = Depends(get_current_principal), data: DataClient = Depends(get_data_client)) -> StreamingResponse:
    return await _event_image(event_id, "crop", principal, data)


@router.get("/api/v1/events/{event_id}/annotated-snapshot")
async def get_event_annotated_snapshot(event_id: str, principal: Principal = Depends(get_current_principal), data: DataClient = Depends(get_data_client)) -> StreamingResponse:
    return await _event_image(event_id, "annotated-snapshot", principal, data)


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
    return _public_event(event)
