from __future__ import annotations

import time
from pathlib import Path


def enforce_retention(
    camera_root: Path,
    max_bytes: int,
    max_age_hours: int,
    now: float | None = None,
) -> list[Path]:
    """Delete oldest completed TS segments until age and size limits are met."""

    current = time.time() if now is None else now
    cutoff = current - max_age_hours * 3600
    files = []
    for path in camera_root.rglob("*.ts") if camera_root.exists() else []:
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        files.append((stat.st_mtime, stat.st_size, path))

    deleted: list[Path] = []
    retained = []
    for mtime, size, path in sorted(files):
        if mtime < cutoff:
            path.unlink(missing_ok=True)
            deleted.append(path)
        else:
            retained.append((mtime, size, path))

    total = sum(size for _, size, _ in retained)
    for _, size, path in retained:
        if total <= max_bytes:
            break
        path.unlink(missing_ok=True)
        total -= size
        deleted.append(path)

    for directory in sorted(camera_root.rglob("*"), reverse=True):
        if directory.is_dir():
            try:
                directory.rmdir()
            except OSError:
                pass
    return deleted
