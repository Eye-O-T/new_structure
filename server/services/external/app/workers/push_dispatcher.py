# Data에서 발송할 알림을 가져와 FCM으로 보내고 성공·재시도 결과를 되돌려 준다.

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
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
        self.running = False
        self.last_error_code: str | None = None
        self.last_sent_at: str | None = None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": True,
            "status": (
                "stopped" if not self.running else
                "degraded" if self.last_error_code else
                "ready" if self.last_sent_at else "waiting"
            ),
            "last_error_code": self.last_error_code,
            "last_sent_at": self.last_sent_at,
            "delivery_confirmed": self.last_sent_at is not None,
        }

    # 작업 하나를 임대받아 발송하고 결과를 Data에 기록하며 빈 대기열은 False로 알린다.
    async def dispatch_once(self) -> bool:
        delivery = await self.data.claim_push()
        if self.last_error_code == "PUSH_QUEUE_UNAVAILABLE":
            self.last_error_code = None
        if delivery is None:
            return False
        # FCM 전송과 Data 완료 기록은 서로 다른 통신이다.
        # 전송 후 기록에 실패하면 다시 발송될 수 있으므로 이벤트 ID를 같은 알림 식별자로 사용한다.
        try:
            await self.sender.send(delivery)
        except PushSendError as exc:
            self.last_error_code = "PUSH_DELIVERY_FAILED"
            await self.data.complete_push(delivery, exc.outcome, exc.code)
        except Exception:
            self.last_error_code = "PUSH_SENDER_UNAVAILABLE"
            await self.data.complete_push(delivery, "retry", "SENDER_UNAVAILABLE")
        else:
            await self.data.complete_push(delivery, "sent")
            self.last_sent_at = datetime.now(timezone.utc).isoformat()
            self.last_error_code = None
        return True

    # 작업이 있으면 빠르게 다음 항목을 처리하고 실패·빈 대기열에는 설정 간격을 적용한다.
    async def run(self) -> None:
        self.running = True
        try:
            while True:
                try:
                    dispatched = await self.dispatch_once()
                except Exception:
                    self.last_error_code = "PUSH_QUEUE_UNAVAILABLE"
                    LOGGER.warning("Push delivery temporarily unavailable")
                    dispatched = False
                # 발송할 항목이 있으면 짧게 쉬고 계속 처리하며, 대기열이 비었을 때는 조회 간격을 늘린다.
                await asyncio.sleep(
                    0.05 if dispatched else self.settings.push_poll_interval_seconds
                )
        finally:
            self.running = False
            await self.sender.close()
