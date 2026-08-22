"""Identifiers and filesystem boundary validation."""

from __future__ import annotations

import re
from pathlib import Path

CAMERA_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def validate_camera_id(value: str) -> str:
    if not CAMERA_ID_PATTERN.fullmatch(value):
        raise ValueError("camera_id must match ^[a-z0-9][a-z0-9_-]{0,63}$")
    return value


def validate_stream_path(value: str) -> str:
    """Validate the single-segment MediaMTX path used by this release."""

    return validate_camera_id(value)


def safe_storage_path(root: str | Path, relative_path: str | Path) -> Path:
    """Resolve a DB path below a storage root and reject traversal/absolute input."""

    relative = Path(relative_path)
    if relative.is_absolute():
        raise ValueError("storage path must be relative")

    base = Path(root).resolve()
    candidate = (base / relative).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ValueError("storage path escapes the configured root") from exc
    return candidate
