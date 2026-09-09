# Data에서 발송할 알림을 가져와 FCM으로 보내고 성공·재시도 결과를 되돌려 준다.

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

    # 작업 하나를 임대받아 발송하고 결과를 Data에 기록하며 빈 대기열은 False로 알린다.
    async def dispatch_once(self) -> bool:
        delivery = await self.data.claim_push()
        if delivery is None:
            return False
        # FCM 전송과 Data 완료 기록은 서로 다른 통신이다.
        # 전송 후 기록에 실패하면 다시 발송될 수 있으므로 이벤트 ID를 같은 알림 식별자로 사용한다.
        try:
            await self.sender.send(delivery)
        except PushSendError as exc:
            await self.data.complete_push(delivery, exc.outcome, exc.code)
        except Exception:
            await self.data.complete_push(delivery, "retry", "SENDER_UNAVAILABLE")
        else:
            await self.data.complete_push(delivery, "sent")
        return True

    # 작업이 있으면 빠르게 다음 항목을 처리하고 실패·빈 대기열에는 설정 간격을 적용한다.
    async def run(self) -> None:
        try:
            while True:
                try:
                    dispatched = await self.dispatch_once()
                except Exception:
                    LOGGER.warning("Push delivery temporarily unavailable")
                    dispatched = False
                # 발송할 항목이 있으면 짧게 쉬고 계속 처리하며, 대기열이 비었을 때는 조회 간격을 늘린다.
                await asyncio.sleep(
                    0.05 if dispatched else self.settings.push_poll_interval_seconds
                )
        finally:
            await self.sender.close()
