# 조회 시간 범위와 식별자를 검사하고 클라이언트가 사용할 영상 주소를 구성한다.
"""Validation shared by resource listing and protected media routes."""

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


def _normalize_time(value: datetime | None, name: str) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise HTTPException(status_code=400, detail=f"{name} must include a timezone")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _validated_time_range(
    start: datetime | None,
    end: datetime | None,
) -> tuple[str | None, str | None]:
    if start is not None and end is not None and start >= end:
        raise HTTPException(status_code=400, detail="start must be earlier than end")
    return _normalize_time(start, "start"), _normalize_time(end, "end")


def _public_media_url(settings: Settings, path: str) -> str:
    return f"{settings.public_base_url}{path}" if settings.public_base_url else path
