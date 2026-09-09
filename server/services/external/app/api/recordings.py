# 녹화 권한을 검사하고 재생 주소 또는 Data가 읽은 영상 바이트를 사용자에게 전달한다.
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import PurePosixPath
from typing import Any, AsyncIterator
from urllib.parse import quote, urlencode

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
)
from fastapi.responses import StreamingResponse

from ai_cctv_core.time import central_recording_start

from ..clients.data import (
    DataClient,
    DataServiceError,
)
from ..config import CAMERA_ID_PATTERN, Settings
from ..dependencies import (
    get_data_client,
    get_settings_dependency,
)
from ..schemas import (
    RecordingPageResponse,
    RecordingPlaybackResponse,
    RecordingResponse,
    RecoveryJobPageResponse,
)
from ..security.permissions import (
    Principal,
    _ensure_camera_access,
    get_current_principal,
    require_admin,
)
from .validation import _public_media_url, _validate_resource_id, _validated_time_range

router = APIRouter()


# DB의 재생 길이를 유지하면서 파일명에 기록된 정밀 시작 시각으로 MediaMTX 주소를 구성한다.
def _playback_url(settings: Settings, segment: dict[str, Any]) -> str:
    camera_id = str(segment.get("camera_id", ""))
    if not CAMERA_ID_PATTERN.fullmatch(camera_id):
        raise DataServiceError("invalid recording response")
    try:
        start = datetime.fromisoformat(
            str(segment["start_time"]).replace("Z", "+00:00")
        )
        end = datetime.fromisoformat(str(segment["end_time"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        raise DataServiceError("invalid recording response") from exc
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise DataServiceError("invalid recording response")
    duration = (end - start).total_seconds()
    # DB의 밀리초 절삭·기존 수정 시각 오차로 파일 시작보다 앞을 요청하면 재생이 실패한다.
    filename = PurePosixPath(str(segment.get("relative_path", ""))).name
    start = central_recording_start(filename) or start
    query = urlencode(
        {
            "path": camera_id,
            "start": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "duration": f"{duration:.3f}",
            "format": "fmp4",
        }
    )
    path = f"{settings.public_playback_prefix}/get?{query}"
    return f"{settings.public_base_url}{path}" if settings.public_base_url else path


# 카메라 권한과 시간대·구간 순서를 확인한 뒤 녹화 검색을 Data에 위임한다.
@router.get("/api/v1/recordings", response_model=RecordingPageResponse)
async def list_recordings(
    camera_id: str = Query(),
    start: datetime = Query(alias="from"),
    end: datetime = Query(alias="to"),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    principal: Principal = Depends(get_current_principal),
    data: DataClient = Depends(get_data_client),
) -> Any:
    await _ensure_camera_access(data, principal, camera_id)
    start_utc, end_utc = _validated_time_range(start, end)
    return await data.list_recordings(
        user_id=principal.user_id,
        camera_id=camera_id,
        start=start_utc,
        end=end_utc,
        limit=limit,
        offset=offset,
    )


# 녹화 ID로 자료를 조회한 후 그 녹화가 속한 카메라의 열람 권한까지 검사한다.
@router.get("/api/v1/recordings/{segment_id}", response_model=RecordingResponse)
async def get_recording(
    segment_id: str,
    principal: Principal = Depends(get_current_principal),
    data: DataClient = Depends(get_data_client),
) -> Any:
    recording = await data.get_recording(
        _validate_resource_id(segment_id),
        user_id=principal.user_id,
    )
    await _ensure_camera_access(data, principal, str(recording.get("camera_id", "")))
    return recording


# 복구 MPEG-TS는 인증된 content API로, 중앙 MP4는 MediaMTX 재생 주소로 안내한다.
@router.get(
    "/api/v1/recordings/{segment_id}/playback",
    response_model=RecordingPlaybackResponse,
)
async def get_recording_playback(
    segment_id: str,
    principal: Principal = Depends(get_current_principal),
    settings: Settings = Depends(get_settings_dependency),
    data: DataClient = Depends(get_data_client),
) -> Any:
    segment_id = _validate_resource_id(segment_id)
    recording = await data.get_recording(segment_id, user_id=principal.user_id)
    await _ensure_camera_access(data, principal, str(recording.get("camera_id", "")))
    if recording.get("format") == "mpegts":
        relative_url = f"/api/v1/recordings/{quote(segment_id, safe='')}/content"
        playback_url = _public_media_url(settings, relative_url)
    else:
        playback_url = _playback_url(settings, recording)
    return {
        "recording_id": segment_id,
        "playback_url": playback_url,
    }


# 카메라 권한을 확인한 뒤 Range·If-Range를 전달하고 필요한 응답 헤더만 중계한다.
@router.get(
    "/api/v1/recordings/{segment_id}/content",
    response_class=StreamingResponse,
    responses={
        200: {
            "description": "Complete recording content",
            "content": {
                "video/mp2t": {"schema": {"type": "string", "format": "binary"}},
                "video/mp4": {"schema": {"type": "string", "format": "binary"}},
            },
        },
        206: {
            "description": "Recording byte range",
            "content": {
                "video/mp2t": {"schema": {"type": "string", "format": "binary"}},
                "video/mp4": {"schema": {"type": "string", "format": "binary"}},
            },
        },
        416: {"description": "Unsatisfiable byte range"},
    },
)
async def get_recording_content(
    segment_id: str,
    request: Request,
    principal: Principal = Depends(get_current_principal),
    data: DataClient = Depends(get_data_client),
) -> StreamingResponse:
    segment_id = _validate_resource_id(segment_id)
    recording = await data.get_recording(segment_id, user_id=principal.user_id)
    await _ensure_camera_access(data, principal, str(recording.get("camera_id", "")))
    upstream = await data.open_recording_content(
        segment_id,
        range_header=request.headers.get("range"),
        if_range_header=request.headers.get("if-range"),
    )
    forwarded_headers = {
        name: value
        for name, value in upstream.headers.items()
        if name.lower()
        in {
            "accept-ranges",
            "cache-control",
            "content-length",
            "content-range",
            "etag",
            "last-modified",
        }
    }

    # 하위 응답을 청크로 전달하며 재생 중단이나 예외에도 Data 스트림을 닫는다.
    async def chunks() -> AsyncIterator[bytes]:
        try:
            if upstream.is_stream_consumed:
                yield upstream.content
            else:
                async for chunk in upstream.aiter_raw():
                    yield chunk
        finally:
            await upstream.aclose()

    return StreamingResponse(
        chunks(),
        status_code=upstream.status_code,
        media_type=upstream.headers.get("content-type", "application/octet-stream"),
        headers=forwarded_headers,
    )


# 관리자만 복구 진행 목록을 조회하게 하며 카메라 필터를 지정하면 ID 형식을 검사한다.
@router.get("/api/v1/recovery-jobs", response_model=RecoveryJobPageResponse)
async def list_recovery_jobs(
    camera_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    _: Principal = Depends(require_admin),
    data: DataClient = Depends(get_data_client),
) -> Any:
    if camera_id is not None and not CAMERA_ID_PATTERN.fullmatch(camera_id):
        raise HTTPException(status_code=400, detail="Invalid camera ID")
    return await data.list_recovery_jobs(
        camera_id=camera_id, limit=limit, offset=offset
    )
