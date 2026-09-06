"""Claim and complete durable deliveries through Data; send through the FCM adapter."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from ..clients.data import DataClient
from ..config import Settings
from ..notifications.firebase import FirebaseSender, PushSendError

LOGGER = logging.getLogger("ai_cctv.push")


class PushDispatcher:
    def __init__(
        self, settings: Settings, data_client: DataClient, sender: Any = None
    ) -> None:
        self.settings = settings
        self.data = data_client
        self.sender = sender or FirebaseSender(settings)

    async def dispatch_once(self) -> bool:
        delivery = await self.data.claim_push()
        if delivery is None:
            return False
        try:
            await self.sender.send(delivery)
        except PushSendError as exc:
            await self.data.complete_push(delivery, exc.outcome, exc.code)
        except Exception:
            await self.data.complete_push(delivery, "retry", "SENDER_UNAVAILABLE")
        else:
            await self.data.complete_push(delivery, "sent")
        return True

    async def run(self) -> None:
        try:
            while True:
                try:
                    dispatched = await self.dispatch_once()
                except Exception:
                    LOGGER.warning("Push delivery temporarily unavailable")
                    dispatched = False
                # Briefly yield between deliveries; wait only when queue is empty.
                await asyncio.sleep(
                    0.05 if dispatched else self.settings.push_poll_interval_seconds
                )
        finally:
            await self.sender.close()
