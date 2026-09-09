"""탐지 이벤트를 디스크에 먼저 저장하고 Data에 재전송하는 영구 발송함."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

import httpx

from ai_cctv_core.contracts.snapshot_protection import (
    MANIFEST_NAME,
    MANIFEST_SCHEMA_VERSION,
    MAX_MANIFEST_BYTES,
    MAX_PROTECTED_EVENTS,
    MAX_PROTECTED_PATHS,
)
from ai_cctv_core.time import format_utc, utc_now

LOGGER = logging.getLogger("ai_cctv.preprocessing")


class EventPublisher:
    def __init__(
        self,
        data_client,
        path: Path,
        max_pending: int = 10000,
        max_bytes: int = 64 * 1024 * 1024,
    ):
        self.data = data_client
        self.path = path
        self.max_pending = max_pending
        self.max_bytes = max_bytes
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error: str | None = None
        self.protection_path = path.parent / MANIFEST_NAME
        self._protected_at = 0.0
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._database() as database:
            database.execute("PRAGMA journal_mode=WAL")
            database.execute(
                "CREATE TABLE IF NOT EXISTS pending_events ("
                "sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
                "payload TEXT NOT NULL, rejected INTEGER NOT NULL DEFAULT 0)"
            )
            database.execute(
                "CREATE TABLE IF NOT EXISTS waiting_observations ("
                "camera_id TEXT PRIMARY KEY, payload TEXT NOT NULL)"
            )
            database.execute("BEGIN IMMEDIATE")
            self._write_protection(database, allow_incomplete=True)

    def _write_protection(self, database, *, allow_incomplete=False):
        """현재 트랜잭션의 보호 목록을 원자적으로 게시한다."""
        paths, events = set(), []
        for (encoded,) in database.execute(
            "SELECT payload FROM pending_events UNION ALL "
            "SELECT payload FROM waiting_observations"
        ):
            event = json.loads(encoded)
            observation = event.get("object_observation") or {}
            for value in (
                event.get("snapshot_path"),
                observation.get("crop_path"),
                observation.get("annotated_snapshot_path"),
            ):
                if isinstance(value, str) and value:
                    paths.add(value)
            if event.get("camera_id") and event.get("source_event_id"):
                events.append(
                    {key: event[key] for key in ("camera_id", "source_event_id")}
                )
        payload = json.dumps(
            {
                "schema_version": MANIFEST_SCHEMA_VERSION,
                "complete": True,
                "generated_at": format_utc(utc_now()),
                "paths": sorted(paths),
                "events": events,
            },
            ensure_ascii=False,
            allow_nan=False,
        )
        if (
            len(paths) > MAX_PROTECTED_PATHS
            or len(events) > MAX_PROTECTED_EVENTS
            or len(payload.encode("utf-8")) > MAX_MANIFEST_BYTES
        ):
            self.last_error = "EVENT_OUTBOX_FULL"
            if not allow_incomplete:
                raise RuntimeError(
                    "outbox protection capacity exceeded; events are retained"
                )
            # 참조를 잘라 정상 목록으로 게시하면 삭제될 수 있다. 작은 보류 표식으로
            # 파일 크기 상한을 유지하고 Data의 이벤트·이미지 정리만 중단한다.
            payload = json.dumps(
                {
                    "schema_version": MANIFEST_SCHEMA_VERSION,
                    "complete": False,
                    "generated_at": format_utc(utc_now()),
                    "paths": [],
                    "events": [],
                }
            )
        descriptor, name = tempfile.mkstemp(
            prefix=".outbox-protection-", dir=self.path.parent
        )
        temporary = Path(name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o644)
            os.replace(temporary, self.protection_path)
        finally:
            temporary.unlink(missing_ok=True)
        self._protected_at = time.monotonic()

    def refresh_protection(self):
        # 다른 생산자의 새 항목을 누락한 목록이 나중에 덮어쓰지 않도록 쓰기 잠금으로 직렬화한다.
        with self._database() as database:
            database.execute("BEGIN IMMEDIATE")
            self._write_protection(database, allow_incomplete=True)

    def _protect_waiting(self, event: dict) -> None:
        """포화로 RAM에서 재시도하는 카메라별 한 건의 이미지 참조를 영속 보존한다."""
        observation = event.get("object_observation") or {}
        protection = {
            "camera_id": event.get("camera_id"),
            "source_event_id": event["source_event_id"],
            "snapshot_path": event.get("snapshot_path"),
            "object_observation": {
                key: observation.get(key)
                for key in ("crop_path", "annotated_snapshot_path")
            },
        }
        with self._database() as database:
            database.execute("BEGIN IMMEDIATE")
            database.execute(
                "INSERT INTO waiting_observations(camera_id,payload) VALUES (?,?) "
                "ON CONFLICT(camera_id) DO UPDATE SET payload=excluded.payload",
                (event.get("camera_id") or "", json.dumps(protection, allow_nan=False)),
            )
            self._write_protection(database, allow_incomplete=True)

    def prune_waiting(self, active_camera_ids: set[str]) -> None:
        """목록 조회에 성공했고 생산자도 종료된 카메라의 고아 참조만 해제한다."""
        with self._database() as database:
            database.execute("BEGIN IMMEDIATE")
            if active_camera_ids:
                placeholders = ",".join("?" for _ in active_camera_ids)
                removed = database.execute(
                    f"DELETE FROM waiting_observations WHERE camera_id NOT IN ({placeholders})",
                    tuple(sorted(active_camera_ids)),
                ).rowcount
            else:
                removed = database.execute("DELETE FROM waiting_observations").rowcount
        if removed:
            # 삭제 commit 실패 시 기존 목록을 유지한다. 갱신 실패도 과잉 보호만 남긴다.
            self.refresh_protection()
            LOGGER.warning(
                "inactive producers' waiting references released; queued events are retained",
                extra={"released_waiting_references": removed},
            )

    @contextmanager
    def _database(self):
        database = None
        try:
            database = sqlite3.connect(self.path, timeout=1)
            database.execute("PRAGMA synchronous=FULL")
            with database:
                yield database
        except (sqlite3.Error, OSError):
            self.last_error = "EVENT_STORAGE"
            raise
        finally:
            if database is not None:
                database.close()

    def submit(self, payload: dict) -> None:
        # 응답 유실 후 같은 이벤트를 재전송해도 Data의 후속 분석 작업이 중복되지 않는다.
        event = dict(payload)
        event.setdefault("source_event_id", uuid.uuid4().hex)
        encoded = json.dumps(event, ensure_ascii=False, allow_nan=False)
        if len(encoded.encode("utf-8")) > 256 * 1024:
            raise ValueError("event exceeds the outbox size limit")
        try:
            with self._database() as database:
                database.execute("BEGIN IMMEDIATE")
                count, size = database.execute(
                    "SELECT count(*),coalesce(sum(length(CAST(payload AS BLOB))),0) "
                    "FROM pending_events"
                ).fetchone()
                if (
                    count >= self.max_pending
                    or size + len(encoded.encode("utf-8")) > self.max_bytes
                ):
                    self.last_error = "EVENT_OUTBOX_FULL"
                    raise RuntimeError(
                        "event outbox is full; undelivered events are retained"
                    )
                database.execute(
                    "INSERT INTO pending_events(payload) VALUES (?)", (encoded,)
                )
                # 카메라마다 한 생산자만 현재 이벤트를 재시도한다. 재시작 전의
                # 고아 참조도 같은 카메라의 다음 영속 등록이 성공할 때 해제한다.
                database.execute(
                    "DELETE FROM waiting_observations WHERE camera_id=?",
                    (event.get("camera_id") or "",),
                )
                self._write_protection(database)
        except RuntimeError:
            # 실패한 큐 트랜잭션 밖에서 확정해야 예외로 참조까지 rollback되지 않는다.
            self._protect_waiting(event)
            raise
        if self.last_error in {"EVENT_STORAGE", "EVENT_OUTBOX_FULL"}:
            self.last_error = None
        self._wake.set()

    def deliver_once(self) -> bool:
        with self._database() as database:
            row = database.execute(
                "SELECT sequence,payload FROM pending_events "
                "WHERE rejected=0 ORDER BY sequence LIMIT 1"
            ).fetchone()
        if row is None:
            return False
        sequence, encoded = row
        try:
            payload = json.loads(encoded)
            self.data.create_event(payload)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code in {400, 404, 413, 422}:
                # 영구 거부 항목은 삭제하지 않고 격리해 다음 카메라의 이벤트를 막지 않는다.
                with self._database() as database:
                    database.execute(
                        "UPDATE pending_events SET rejected=1 WHERE sequence=?",
                        (sequence,),
                    )
                self.last_error = "EVENT_REJECTED"
                LOGGER.warning("event retained after permanent rejection")
                return True
            self.last_error = "EVENT_DELIVERY"
            raise
        except Exception:
            self.last_error = "EVENT_DELIVERY"
            raise
        with self._database() as database:
            database.execute("BEGIN IMMEDIATE")
            database.execute("DELETE FROM pending_events WHERE sequence=?", (sequence,))
        # 삭제는 먼저 확정한다. commit 실패·중단에도 기존 목록이 남아 재전송을 보호한다.
        # 후속 갱신 실패는 이미 전달한 항목을 더 오래 보호할 뿐이며 다음 heartbeat가 복구한다.
        self.refresh_protection()
        self.last_error = None
        return True

    def status(self) -> dict:
        try:
            with self._database() as database:
                pending, rejected = database.execute(
                    "SELECT count(*)-coalesce(sum(rejected),0),coalesce(sum(rejected),0) "
                    "FROM pending_events"
                ).fetchone()
                waiting = database.execute(
                    "SELECT count(*) FROM waiting_observations"
                ).fetchone()[0]
        except (sqlite3.Error, OSError):
            # 저장소 장애가 상태 API까지 실패시키지 않게 하고, 알 수 없는 건수는 null로 둔다.
            pending = rejected = waiting = None
        return {
            "pending": pending,
            "rejected": rejected,
            "waiting": waiting,
            "last_error": self.last_error or ("EVENT_OUTBOX_FULL" if waiting else None),
        }

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="event-delivery", daemon=True
        )
        self._thread.start()

    def _run(self) -> None:
        delay = 0.5
        while not self._stop.is_set():
            self._wake.clear()
            try:
                if time.monotonic() - self._protected_at >= 30:
                    self.refresh_protection()
                worked = self.deliver_once()
            except Exception:
                LOGGER.warning(
                    "event delivery deferred; persisted events will be retried"
                )
                self._stop.wait(delay)
                delay = min(delay * 2, 30)
                continue
            delay = 0.5
            if not worked:
                self._wake.wait(0.5)

    def close(self, timeout: float = 5.0) -> None:
        # 중단 때 발송함을 비우지 않는다. 남은 항목은 다음 프로세스가 이어서 보낸다.
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=max(0, timeout))
