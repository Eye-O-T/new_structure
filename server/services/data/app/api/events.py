# 이벤트와 객체 관찰 정보를 검증하고 녹화 연결 및 후속 작업 저장을 요청한다.

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Query,
    status,
)

from ai_cctv_core.time import format_utc, parse_utc

from ..config import Settings
from ..dependencies import Repo, RuntimeSettings, _page
from ..errors import ApiError, _not_found
from ..schemas import (
    EventCreate,
)
from ..storage.paths import normalize_relative_path

router = APIRouter()


# 이벤트 전후 녹화 구간과 안전한 스냅샷 경로를 만들고 객체 분석의 초기 상태를 채운다.
def _event_values(payload: EventCreate, settings: Settings) -> dict[str, Any]:
    values = payload.model_dump()
    values["occurred_at"] = format_utc(payload.occurred_at)
    values["link_start_at"] = format_utc(
        payload.occurred_at - timedelta(seconds=settings.event_pre_roll_seconds)
    )
    values["link_end_at"] = format_utc(
        payload.occurred_at + timedelta(seconds=settings.event_post_roll_seconds)
    )
    if payload.snapshot_path is not None:
        relative, _target = normalize_relative_path(
            settings.snapshot_root, payload.snapshot_path
        )
        values["snapshot_path"] = relative
    if payload.object_observation is not None:
        if not payload.person_id or payload.event_type != "person_appeared":
            raise ApiError(
                422,
                "INVALID_OBJECT_EVENT",
                "Object observations require a person appearance",
            )
        observation = payload.object_observation.model_dump(mode="json")
        for key in ("crop_path", "annotated_snapshot_path"):
            if observation.get(key):
                relative, _ = normalize_relative_path(
                    settings.snapshot_root, observation[key]
                )
                observation[key] = relative
        values["object_observation"] = observation
        values["metadata"]["object"] = observation
        values["metadata"]["tracking_session_id"] = observation["tracking_session_id"]
        values["metadata"]["identity"] = {"status": "pending"}
        values["metadata"]["analysis"] = {"status": "pending"}
    return values


# 카메라를 확인하고 이벤트를 저장한 뒤 연결 변화라면 복구 구간도 갱신한다.
@router.post("/events", status_code=status.HTTP_201_CREATED)
def create_event(
    payload: EventCreate, repository: Repo, settings: RuntimeSettings
) -> dict[str, Any]:
    if repository.get_camera(payload.camera_id) is None:
        raise _not_found("camera")
    values = _event_values(payload, settings)
    event = repository.create_event(values)
    event_type = (
        payload.event_type.value
        if hasattr(payload.event_type, "value")
        else str(payload.event_type)
    )
    repository.note_recovery_event(
        camera_id=payload.camera_id,
        event_type=event_type,
        occurred_at=values["occurred_at"],
        max_attempts=settings.recovery_max_attempts,
        settle_seconds=settings.recovery_settle_seconds,
    )
    return event


# 시간대가 있는 입력만 공통 UTC 문자열로 바꾸어 저장소 검색에 전달한다.
@router.get("/events")
def search_events(
    repository: Repo,
    camera_id: str | None = None,
    event_type: str | None = None,
    from_time: Annotated[datetime | None, Query(alias="from")] = None,
    to_time: Annotated[datetime | None, Query(alias="to")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    start = parse_utc(from_time) if from_time is not None else None
    end = parse_utc(to_time) if to_time is not None else None
    if start is not None and end is not None and end <= start:
        raise ApiError(422, "INVALID_TIME_RANGE", "to는 from보다 뒤여야 합니다.")
    items = repository.search_events(
        camera_id=camera_id,
        event_type=event_type,
        start_time=format_utc(start) if start else None,
        end_time=format_utc(end) if end else None,
        limit=limit,
        offset=offset,
    )
    return _page(items, limit, offset)


@router.get("/events/{event_id}")
def get_event(event_id: int, repository: Repo) -> dict[str, Any]:
    event = repository.get_event(event_id)
    if event is None:
        raise _not_found("event")
    return event
