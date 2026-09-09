# 녹화 완료 통지를 받아 파일을 등록하고 검색 결과와 영상 내용을 다른 서비스에 제공한다.

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


# 파일 검증 후 등록하며 재전송에서도 이벤트 연결을 재시도하고 중복 여부를 응답에 표시한다.
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


# 순서가 올바른 UTC 구간으로 검색하고 일관된 페이지 형식으로 결과를 반환한다.
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


# ready 상태와 실제 파일 존재를 확인한 뒤 범위 요청을 지원하는 파일 응답을 만든다.
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


# MediaMTX 폼 통지를 실제 파일 메타데이터로 바꾸고 관련 이벤트에 연결한다.
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
