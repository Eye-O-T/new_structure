# 로컬 TS 백업을 보관 시간, 총 용량 순서로 정리해 Edge 디스크가 계속 차는 것을 막는다.
from __future__ import annotations

import time
from pathlib import Path


def enforce_retention(
    camera_root: Path,
    max_bytes: int,
    max_age_hours: int,
    now: float | None = None,
    *,
    preserve_newest: bool = False,
) -> list[Path]:
    """Delete old TS segments without unlinking the active splitmux output."""

    current = time.time() if now is None else now
    cutoff = current - max_age_hours * 3600
    files = []
    for path in camera_root.rglob("*.ts") if camera_root.exists() else []:
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        files.append((stat.st_mtime, stat.st_size, path))

    protected = None
    # 캡처 중에는 가장 최근 파일이 아직 열려 있을 수 있어 용량 한도보다 파일 보호를 우선한다.
    if preserve_newest and files:
        protected = max(files, key=lambda item: (item[0], item[2].as_posix()))[2]

    deleted: list[Path] = []
    retained = []
    for mtime, size, path in sorted(files):
        if path != protected and mtime < cutoff:
            path.unlink(missing_ok=True)
            deleted.append(path)
        else:
            retained.append((mtime, size, path))

    total = sum(size for _, size, _ in retained)
    for _, size, path in retained:
        if total <= max_bytes:
            break
        if path == protected:
            continue
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
