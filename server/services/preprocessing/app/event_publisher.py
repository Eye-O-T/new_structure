"""탐지 이벤트를 디스크에 먼저 저장하고 Data에 재전송하는 영구 발송함."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from pathlib import Path

import httpx

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
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._database() as database:
            database.execute("PRAGMA journal_mode=WAL")
            database.execute(
                "CREATE TABLE IF NOT EXISTS pending_events ("
                "sequence INTEGER PRIMARY KEY AUTOINCREMENT, "
                "payload TEXT NOT NULL, rejected INTEGER NOT NULL DEFAULT 0)"
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
            database.execute("DELETE FROM pending_events WHERE sequence=?", (sequence,))
        self.last_error = None
        return True

    def status(self) -> dict:
        try:
            with self._database() as database:
                pending, rejected = database.execute(
                    "SELECT count(*)-coalesce(sum(rejected),0),coalesce(sum(rejected),0) "
                    "FROM pending_events"
                ).fetchone()
        except (sqlite3.Error, OSError):
            # 저장소 장애가 상태 API까지 실패시키지 않게 하고, 알 수 없는 건수는 null로 둔다.
            pending = rejected = None
        return {"pending": pending, "rejected": rejected, "last_error": self.last_error}

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
