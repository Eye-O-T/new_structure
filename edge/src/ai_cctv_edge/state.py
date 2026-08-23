from __future__ import annotations

import json
import math
import os
import re
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import VIDEO_PROFILES, write_atomic

try:  # Linux deployment; the fallback keeps imports/test doubles portable.
    import fcntl
except ImportError:  # pragma: no cover - exercised only on non-POSIX hosts
    fcntl = None  # type: ignore[assignment]


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def default_state_root() -> Path:
    return Path(
        os.environ.get("AI_CCTV_EDGE_STATE_ROOT", "/var/lib/ai-cctv-edge/state")
    )


def default_runtime_root() -> Path:
    return Path(os.environ.get("AI_CCTV_EDGE_RUNTIME_ROOT", "/run/ai-cctv-edge"))


class RuntimeStatusStore:
    def __init__(self, root: Path | None = None):
        self.root = root or default_state_root()
        self.path = self.root / "status.json"

    def read(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def write(self, payload: dict[str, Any]) -> None:
        write_atomic(
            self.path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            mode=0o640,
        )


class ProfileSelectionStore:
    """Persistent, atomically replaced runtime video-profile selection."""

    def __init__(self, root: Path | None = None):
        self.root = root or default_state_root()
        self.path = self.root / "video-profile.json"

    def read(self, default_profile: str) -> tuple[str, int]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            profile = str(payload["profile"])
            generation = int(payload["generation"])
            if profile not in VIDEO_PROFILES or generation < 0:
                raise ValueError
            return profile, generation
        except (
            OSError,
            KeyError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ):
            return default_profile, 0

    def write(self, profile: str, generation: int) -> None:
        if profile not in VIDEO_PROFILES:
            raise ValueError(f"unsupported video profile: {profile}")
        if generation < 0:
            raise ValueError("profile generation cannot be negative")
        write_atomic(
            self.path,
            json.dumps(
                {
                    "profile": profile,
                    "generation": generation,
                    "updated_at": utc_timestamp(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            mode=0o640,
        )


class ProfileRequestStore:
    """Transient request scoped to one live runner instance."""

    def __init__(self, root: Path | None = None):
        self.root = root or default_runtime_root()
        self.path = self.root / "video-profile-request.json"

    def read(self) -> dict[str, Any] | None:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            profile = str(payload["profile"])
            generation = int(payload["generation"])
            runner_pid = int(payload["runner_pid"])
            runner_instance_id = str(payload["runner_instance_id"])
            requested_monotonic = float(payload["requested_monotonic"])
            if (
                profile not in VIDEO_PROFILES
                or generation < 0
                or runner_pid <= 0
                or len(runner_instance_id) < 16
                or not math.isfinite(requested_monotonic)
                or requested_monotonic < 0
            ):
                raise ValueError
            return {
                "profile": profile,
                "generation": generation,
                "runner_pid": runner_pid,
                "runner_instance_id": runner_instance_id,
                "requested_monotonic": requested_monotonic,
            }
        except (
            OSError,
            KeyError,
            ValueError,
            TypeError,
            json.JSONDecodeError,
        ):
            return None

    def write(
        self,
        profile: str,
        generation: int,
        runner_pid: int,
        runner_instance_id: str,
    ) -> None:
        if profile not in VIDEO_PROFILES:
            raise ValueError(f"unsupported video profile: {profile}")
        if generation < 0 or runner_pid <= 0 or len(runner_instance_id) < 16:
            raise ValueError("invalid transient profile request")
        write_atomic(
            self.path,
            json.dumps(
                {
                    "profile": profile,
                    "generation": generation,
                    "runner_pid": runner_pid,
                    "runner_instance_id": runner_instance_id,
                    "requested_at": utc_timestamp(),
                    "requested_monotonic": time.monotonic(),
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            mode=0o640,
        )

    def clear(
        self,
        *,
        generation: int | None = None,
        runner_instance_id: str | None = None,
    ) -> None:
        current = self.read()
        if current is None:
            return
        if generation is not None and current["generation"] != generation:
            return
        if (
            runner_instance_id is not None
            and current["runner_instance_id"] != runner_instance_id
        ):
            return
        self.path.unlink(missing_ok=True)


class EventJournal:
    """Append-only local event journal shared by capture and control services."""

    def __init__(
        self,
        camera_id: str,
        root: Path | None = None,
        max_bytes: int = 2 * 1024 * 1024,
    ):
        if re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", camera_id) is None:
            raise ValueError("camera_id is invalid")
        self.camera_id = camera_id
        self.root = root or default_state_root()
        self.path = self.root / f"events-{camera_id}.jsonl"
        # Read the pre-0.3 shared file for upgrade compatibility, but filter it
        # by camera. New writes are always isolated per camera.
        self.legacy_path = self.root / "events.jsonl"
        self.lock_path = self.root / f"events-{camera_id}.lock"
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    @contextmanager
    def _process_lock(self, *, exclusive: bool):
        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as handle:
            if fcntl is not None:
                operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                fcntl.flock(handle.fileno(), operation)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def record(self, event_type: str, **details: Any) -> dict[str, Any]:
        payload = {
            "event_id": uuid.uuid4().hex,
            "event_type": event_type,
            "camera_id": self.camera_id,
            "occurred_at": utc_timestamp(),
            **details,
        }
        encoded = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        self.root.mkdir(parents=True, exist_ok=True)
        with self._lock:
            with self._process_lock(exclusive=True):
                descriptor = os.open(
                    self.path,
                    os.O_APPEND | os.O_CREAT | os.O_WRONLY,
                    0o640,
                )
                try:
                    os.write(descriptor, encoded)
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                self._compact_if_needed()
        return payload

    def _compact_if_needed(self) -> None:
        try:
            if self.path.stat().st_size <= self.max_bytes:
                return
            lines = self.path.read_bytes().splitlines(keepends=True)
        except OSError:
            return
        retained: list[bytes] = []
        size = 0
        target = self.max_bytes // 2
        for line in reversed(lines):
            if retained and size + len(line) > target:
                break
            retained.append(line)
            size += len(line)
        write_atomic(self.path, b"".join(reversed(retained)).decode("utf-8"))

    def read(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.page(limit=limit)[0]

    def page(
        self,
        *,
        after: str | None = None,
        limit: int = 100,
    ) -> tuple[list[dict[str, Any]], str | None, bool]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be in range 1..1000")
        with self._lock:
            with self._process_lock(exclusive=False):
                lines: list[str] = []
                for path in (self.legacy_path, self.path):
                    try:
                        lines.extend(path.read_text(encoding="utf-8").splitlines())
                    except FileNotFoundError:
                        continue
        all_items: list[dict[str, Any]] = []
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and item.get("camera_id") == self.camera_id:
                all_items.append(item)

        cursor_expired = False
        start = 0
        if after:
            matching = next(
                (
                    index
                    for index, item in enumerate(all_items)
                    if item.get("event_id") == after
                ),
                None,
            )
            if matching is None:
                cursor_expired = bool(all_items)
            else:
                start = matching + 1
        items = all_items[start : start + limit]
        next_cursor = str(items[-1].get("event_id")) if items else after
        return items, next_cursor, cursor_expired
