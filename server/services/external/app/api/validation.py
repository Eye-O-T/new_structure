# 조회 시간 범위와 식별자를 검사하고 클라이언트가 사용할 영상 주소를 구성한다.

from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import HTTPException

from ..config import Settings

RESOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def _validate_resource_id(value: str) -> str:
    if not RESOURCE_ID_PATTERN.fullmatch(value):
        raise HTTPException(status_code=400, detail="Invalid resource ID")
    return value


# 시간대 없는 입력을 거절하고 다른 시간대의 조회 시각을 UTC로 통일한다.
def _normalize_time(value: datetime | None, name: str) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise HTTPException(status_code=400, detail=f"{name} must include a timezone")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


# 선택적 시작·종료를 정규화하되 둘 다 있으면 시작이 종료보다 앞서도록 요구한다.
def _validated_time_range(
    start: datetime | None,
    end: datetime | None,
) -> tuple[str | None, str | None]:
    start_utc = _normalize_time(start, "start")
    end_utc = _normalize_time(end, "end")
    if start is not None and end is not None and start >= end:
        raise HTTPException(status_code=400, detail="start must be earlier than end")
    return start_utc, end_utc


# 공개 origin이 설정되면 절대 주소를, 없으면 동일 출처 상대 주소를 반환한다.
def _public_media_url(settings: Settings, path: str) -> str:
    return f"{settings.public_base_url}{path}" if settings.public_base_url else path
