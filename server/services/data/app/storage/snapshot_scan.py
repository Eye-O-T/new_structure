"""파일 개수·시간이 제한된 스냅샷 탐색. SQLite에는 순회 위치만 저장한다."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import threading
import time
from typing import Any

from ai_cctv_core.time import format_utc, utc_now

MAX_SCAN_ENTRIES = 5000
MAX_SCAN_SECONDS = 2.0
MAX_DEPTH = 32


def is_snapshot_asset(name: str) -> bool:
    # snapshots에는 outbox SQLite와 보호 manifest도 있다. 이미지·알려진 임시 파일만 정리한다.
    return Path(name).suffix.lower() in {".jpg", ".jpeg"} or name.startswith(
        (".snapshot-", ".outbox-protection-")
    )


@dataclass
class _Directory:
    relative: str
    offset: int = 0
    skip: int = 0
    iterator: Any = None


class SnapshotScanner:
    def __init__(self, root: Path, database):
        self.root = root.resolve()
        self.database = database
        self.lock = threading.Lock()
        self.stack: list[_Directory] | None = None

    def _restore(self) -> None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT value_json FROM maintenance_cursors WHERE name='snapshots'"
            ).fetchone()
        self.stack = []
        if row:
            try:
                value = json.loads(row[0])
                if (
                    value["root"] == str(self.root)
                    and 0 < len(value["stack"]) <= MAX_DEPTH
                ):
                    for item in value["stack"]:
                        path = (self.root / item["path"]).resolve()
                        path.relative_to(self.root)
                        offset = int(item["offset"])
                        if offset < 0:
                            raise ValueError("invalid offset")
                        self.stack.append(_Directory(item["path"], offset, offset))
            except (KeyError, TypeError, ValueError):
                self.stack = []
        if not self.stack:
            self.stack = [_Directory(".")]

    def scan(
        self, *, candidate_limit: int = 1000
    ) -> tuple[list[Path], dict[str, object]]:
        with self.lock:
            if self.stack is None:
                self._restore()
            candidates: list[Path] = []
            examined = 0
            started = time.monotonic()
            complete = False
            while (
                self.stack
                and examined < MAX_SCAN_ENTRIES
                and len(candidates) < candidate_limit
                and time.monotonic() - started < MAX_SCAN_SECONDS
            ):
                frame = self.stack[-1]
                try:
                    if frame.iterator is None:
                        directory = (self.root / frame.relative).resolve()
                        directory.relative_to(self.root)
                        frame.iterator = os.scandir(directory)
                    entry = next(frame.iterator)
                except (StopIteration, FileNotFoundError, NotADirectoryError):
                    if frame.iterator is not None:
                        frame.iterator.close()
                    self.stack.pop()
                    continue
                examined += 1
                if frame.skip:
                    frame.skip -= 1
                    continue
                frame.offset += 1
                if entry.is_symlink():
                    continue
                if entry.is_dir(follow_symlinks=False):
                    if len(self.stack) < MAX_DEPTH:
                        self.stack.append(
                            _Directory(
                                Path(entry.path).relative_to(self.root).as_posix()
                            )
                        )
                elif entry.is_file(follow_symlinks=False) and is_snapshot_asset(
                    entry.name
                ):
                    candidates.append(Path(entry.path))
            if not self.stack:
                complete = True
                self.stack = [_Directory(".")]
            value = {
                "root": str(self.root),
                "stack": [{"path": f.relative, "offset": f.offset} for f in self.stack],
            }
            with self.database.transaction() as connection:
                connection.execute(
                    "INSERT INTO maintenance_cursors VALUES ('snapshots',?,?) "
                    "ON CONFLICT(name) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at",
                    (json.dumps(value), format_utc(utc_now())),
                )
            return candidates, {"examined": examined, "cycle_complete": complete}

    def close(self) -> None:
        with self.lock:
            for frame in self.stack or []:
                if frame.iterator is not None:
                    frame.iterator.close()
            self.stack = None
