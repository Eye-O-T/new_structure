"""백그라운드 작업의 재시작과 상태 점검을 제공한다."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from dataclasses import dataclass
from typing import Awaitable, Callable

from ai_cctv_core.time import format_utc, utc_now

LOGGER = logging.getLogger("ai_cctv.data")


@dataclass
class WorkerStatus:
    alive: bool = False
    status: str = "starting"
    last_success_at: str | None = None
    last_error: str | None = None

    def succeeded(self) -> None:
        self.status = "ok"
        self.last_success_at = format_utc(utc_now())
        self.last_error = None

    def failed(self, error: Exception) -> None:
        self.status = "error"
        # 예외 원문에는 주소나 로컬 경로가 포함될 수 있어 점검 API에는 고정 코드만 보낸다.
        self.last_error = (
            "DATABASE_UNAVAILABLE"
            if isinstance(error, sqlite3.Error)
            else "WORKER_ERROR"
        )

    def snapshot(self) -> dict[str, object]:
        return {
            "alive": self.alive,
            "status": self.status,
            "last_success_at": self.last_success_at,
            "last_error": self.last_error,
        }


async def supervise(
    operation: Callable[[], Awaitable[None]],
    state: WorkerStatus,
    *,
    retry_seconds: float = 5.0,
) -> None:
    try:
        while True:
            state.alive = True
            try:
                await operation()
                raise RuntimeError("background worker exited unexpectedly")
            except Exception as exc:
                state.failed(exc)
                LOGGER.exception("restarting failed Data background worker")
            await asyncio.sleep(retry_seconds)
    finally:
        state.alive = False
