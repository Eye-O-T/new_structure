"""Preprocessing의 미전송 이벤트 보호 목록을 보수적으로 해석한다."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import PurePosixPath, PureWindowsPath
import re

from ai_cctv_core.time import parse_utc, utc_now
from ai_cctv_core.contracts.snapshot_protection import (
    MANIFEST_NAME,
    MANIFEST_SCHEMA_VERSION,
    MAX_MANIFEST_BYTES,
    MAX_PROTECTED_EVENTS,
    MAX_PROTECTED_PATHS,
)

from ..config import Settings

MAX_MANIFEST_AGE_SECONDS = 120


@dataclass(frozen=True)
class SnapshotProtection:
    ready: bool
    reason: str | None = None
    paths: frozenset[str] = field(default_factory=frozenset)
    events: frozenset[tuple[str, str]] = field(default_factory=frozenset)
    generated_at: str | None = None

    def status(self) -> dict[str, object]:
        return {
            "status": "ready" if self.ready else "deferred",
            "reason": self.reason,
            "generated_at": self.generated_at,
            "protected_paths": len(self.paths),
            "protected_events": len(self.events),
        }


def _check_age(protection: SnapshotProtection) -> SnapshotProtection:
    if protection.ready and protection.generated_at is not None:
        age = (utc_now() - parse_utc(protection.generated_at)).total_seconds()
        if not -5 <= age <= MAX_MANIFEST_AGE_SECONDS:
            return SnapshotProtection(
                False, "OUTBOX_MANIFEST_STALE", generated_at=protection.generated_at
            )
    return protection


class SnapshotProtectionReader:
    """같은 원자적 manifest를 반복 파싱하지 않되 매번 교체 여부·유효 시간을 확인한다."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.signature = None
        self.cached: SnapshotProtection | None = None

    def read(self) -> SnapshotProtection:
        path = self.settings.snapshot_root / MANIFEST_NAME
        try:
            stat = path.stat()
            signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
        except OSError:
            self.signature = None
            return read_snapshot_protection(self.settings)
        if signature == self.signature and self.cached is not None:
            return _check_age(self.cached)
        protection = read_snapshot_protection(self.settings)
        # stat과 read 사이에 교체되었더라도 다음 확인은 다른 signature를 보고 새 내용을 읽는다.
        self.signature, self.cached = signature, protection
        return _check_age(protection)


def read_snapshot_protection(settings: Settings) -> SnapshotProtection:
    path = settings.snapshot_root / MANIFEST_NAME
    try:
        with path.open("rb") as stream:
            raw = stream.read(MAX_MANIFEST_BYTES + 1)
    except FileNotFoundError:
        return SnapshotProtection(False, "OUTBOX_MANIFEST_MISSING")
    except OSError:
        return SnapshotProtection(False, "OUTBOX_MANIFEST_UNREADABLE")
    try:
        if len(raw) > MAX_MANIFEST_BYTES:
            raise ValueError("manifest too large")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("invalid schema")
        version = value.get("schema_version")
        if type(version) is not int or version not in {1, MANIFEST_SCHEMA_VERSION}:
            raise ValueError("invalid schema")
        generated_at = value["generated_at"]
        age = (utc_now() - parse_utc(generated_at)).total_seconds()
        if not -5 <= age <= MAX_MANIFEST_AGE_SECONDS:
            return SnapshotProtection(
                False, "OUTBOX_MANIFEST_STALE", generated_at=generated_at
            )
        complete = value.get("complete", True if version == 1 else None)
        if not isinstance(complete, bool):
            raise ValueError("invalid completeness flag")
        if not complete:
            return SnapshotProtection(
                False, "OUTBOX_MANIFEST_INCOMPLETE", generated_at=generated_at
            )
        raw_paths, raw_events = value["paths"], value["events"]
        if not isinstance(raw_paths, list) or not isinstance(raw_events, list):
            raise ValueError("invalid items")
        if (
            len(raw_paths) > MAX_PROTECTED_PATHS
            or len(raw_events) > MAX_PROTECTED_EVENTS
        ):
            raise ValueError("too many items")
        paths = set()
        for item in raw_paths:
            if not isinstance(item, str) or not 1 <= len(item) <= 4096:
                raise ValueError("invalid path")
            candidate = PurePosixPath(item)
            if (
                candidate.is_absolute()
                or PureWindowsPath(item).drive
                or "\\" in item
                or ".." in candidate.parts
                or str(candidate) == "."
            ):
                raise ValueError("invalid relative path")
            paths.add(candidate.as_posix())
        events = set()
        for item in raw_events:
            camera, source = item["camera_id"], item["source_event_id"]
            if (
                not isinstance(camera, str)
                or not 1 <= len(camera) <= 256
                or not isinstance(source, str)
                or not re.fullmatch(r"[a-f0-9]{32}", source)
            ):
                raise ValueError("invalid event")
            events.add((camera, source))
        return SnapshotProtection(
            True,
            paths=frozenset(paths),
            events=frozenset(events),
            generated_at=generated_at,
        )
    except (KeyError, TypeError, ValueError, UnicodeError):
        return SnapshotProtection(False, "OUTBOX_MANIFEST_INVALID")
