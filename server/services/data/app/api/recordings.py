"""Internal recordings API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Form,
    Query,
    status,
)
from fastapi.responses import FileResponse

from ai_cctv_core.identifiers import validate_camera_id
from ai_cctv_core.time import format_utc, parse_utc

from ..dependencies import Repo, RuntimeSettings, _page
from ..errors import ApiError, _not_found
from ..schemas import (
    RecordingSegmentCreate,
)
from ..storage.paths import normalize_relative_path
from ..storage.recordings import prepare_recording_hook, prepare_segment

router = APIRouter()


@router.post("/recording-segments", status_code=status.HTTP_201_CREATED)
def create_recording_segment(
    payload: RecordingSegmentCreate,
    repository: Repo,
    settings: RuntimeSettings,
) -> dict[str, Any]:
    segment, created = repository.create_segment(prepare_segment(payload, settings))
    repository.link_segment_to_events(
        segment,
        settings.event_pre_roll_seconds,
        settings.event_post_roll_seconds,
    )
    segment["idempotent_replay"] = not created
    return segment


@router.get("/recording-segments/search")
def search_recording_segments(
    repository: Repo,
    camera_id: str,
    from_time: Annotated[datetime, Query(alias="from")],
    to_time: Annotated[datetime, Query(alias="to")],
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    start = parse_utc(from_time)
    end = parse_utc(to_time)
    if end <= start:
        raise ApiError(422, "INVALID_TIME_RANGE", "to는 from보다 뒤여야 합니다.")
    items = repository.search_segments(
        camera_id, format_utc(start), format_utc(end), limit, offset
    )
    return _page(items, limit, offset)


@router.get("/recording-segments/{segment_id}")
def get_recording_segment(segment_id: int, repository: Repo) -> dict[str, Any]:
    segment = repository.get_segment(segment_id)
    if segment is None:
        raise _not_found("recording")
    return segment


@router.get("/recording-segments/{segment_id}/content")
def get_recording_segment_content(
    segment_id: int,
    repository: Repo,
    settings: RuntimeSettings,
) -> FileResponse:
    segment = repository.get_segment(segment_id)
    if segment is None:
        raise _not_found("recording")
    if segment.get("status") != "ready":
        raise ApiError(
            409,
            "RECORDING_NOT_READY",
            "The recording content is not ready for playback.",
        )
    _relative_path, target = normalize_relative_path(
        settings.storage_root, str(segment["relative_path"])
    )
    if not target.is_file():
        raise ApiError(
            404,
            "RECORDING_FILE_NOT_FOUND",
            "The recording content file was not found.",
        )
    media_type = "video/mp2t" if segment.get("format") == "mpegts" else "video/mp4"
    return FileResponse(
        target,
        media_type=media_type,
        headers={"Cache-Control": "private, no-store"},
    )


@router.post("/hooks/recording-complete", status_code=status.HTTP_201_CREATED)
def recording_complete_hook(
    repository: Repo,
    settings: RuntimeSettings,
    camera_id: Annotated[str, Form()],
    segment_path: Annotated[str, Form()],
    duration_seconds: Annotated[float, Form(gt=0)],
) -> dict[str, Any]:
    validate_camera_id(camera_id)
    segment, created = repository.create_segment(
        prepare_recording_hook(
            camera_id=camera_id,
            segment_path=segment_path,
            duration_seconds=duration_seconds,
            settings=settings,
        )
    )
    repository.link_segment_to_events(
        segment,
        settings.event_pre_roll_seconds,
        settings.event_post_roll_seconds,
    )
    segment["idempotent_replay"] = not created
    return segment
