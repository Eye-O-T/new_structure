# API 요청과 별개로 주기적으로 녹화 목록을 대조하고 보관 정책을 적용한다.
"""Periodic storage reconciliation and retention."""

from __future__ import annotations

import asyncio
import logging

from ..config import Settings
from ..database.repositories import DataRepository
from ..schemas import (
    RetentionRequest,
)
from ..storage.recordings import reconcile
from ..storage.retention import retention_cleanup

LOGGER = logging.getLogger("ai_cctv.data")


async def maintain_storage(repository: DataRepository, settings: Settings) -> None:
    while True:
        await asyncio.sleep(settings.maintenance_interval_seconds)
        try:
            await asyncio.to_thread(reconcile, repository, settings)
            await asyncio.to_thread(
                retention_cleanup,
                repository,
                settings,
                RetentionRequest(
                    retention_days=settings.retention_days,
                    dry_run=False,
                ),
            )
        except Exception:
            LOGGER.exception("scheduled storage maintenance failed")
