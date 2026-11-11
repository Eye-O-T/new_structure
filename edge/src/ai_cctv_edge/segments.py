"""Completion metadata for finalized local recording segments."""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from .config import write_atomic

METADATA_SCHEMA_VERSION = 1


def metadata_path(segment: Path) -> Path:
    return segment.with_suffix(segment.suffix + ".json")


def format_utc(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("segment timestamps must include a timezone")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def segment_overlaps(
    start_time: datetime,
    end_time: datetime,
    query_start: datetime,
    query_end: datetime,
) -> bool:
    """Use half-open interval overlap so adjacent segments do not overlap."""
    return start_time < query_end and end_time > query_start


def write_segment_metadata(
    segment: Path,
    *,
    camera_id: str,
    start_time: datetime,
    end_time: datetime,
) -> Path:
    if end_time <= start_time:
        raise ValueError("segment end time must be after start time")
    target = metadata_path(segment)
    payload = {
        "schema_version": METADATA_SCHEMA_VERSION,
        "camera_id": camera_id,
        "start_time": format_utc(start_time),
        "end_time": format_utc(end_time),
        "complete": True,
    }
    write_atomic(target, json.dumps(payload, separators=(",", ":")) + "\n")
    return target


def read_segment_metadata(
    segment: Path,
    *,
    camera_id: str,
) -> dict[str, object] | None:
    try:
        payload = json.loads(metadata_path(segment).read_text(encoding="utf-8"))
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != METADATA_SCHEMA_VERSION
            or payload.get("camera_id") != camera_id
            or payload.get("complete") is not True
        ):
            return None
        start_time = datetime.fromisoformat(
            str(payload["start_time"]).replace("Z", "+00:00")
        )
        end_time = datetime.fromisoformat(
            str(payload["end_time"]).replace("Z", "+00:00")
        )
        if (
            start_time.tzinfo is None
            or end_time.tzinfo is None
            or end_time <= start_time
        ):
            return None
    except (OSError, UnicodeError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None
    return payload
