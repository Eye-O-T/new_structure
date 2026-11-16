"""Small process-local cache for recording-file status checks."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

_TTL_SECONDS = 2.0
_CACHE: dict[tuple[Path, str], tuple[float, bool]] = {}
_CACHE_LOCK = threading.Lock()


def invalidate_active_recording_file_cache(camera_id: str | None = None) -> None:
    with _CACHE_LOCK:
        if camera_id is None:
            _CACHE.clear()
            return
        for key in tuple(_CACHE):
            if key[1] == camera_id:
                del _CACHE[key]


def active_recording_file_cached(
    camera_id: str,
    storage_root: Path,
    inspect: Callable[[], bool],
) -> bool:
    key = (storage_root.resolve(), camera_id)
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached is not None and time.monotonic() < cached[0]:
            return cached[1]
        active = inspect()
        _CACHE[key] = (time.monotonic() + _TTL_SECONDS, active)
        return active
