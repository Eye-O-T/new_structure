# 알림 대기열 항목을 FCM 메시지로 변환하고 실패가 재시도 가능한지 구분한다.

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any

from ..config import Settings


class PushSendError(Exception):
    def __init__(self, outcome: str, code: str) -> None:
        super().__init__(code)
        self.outcome = outcome
        self.code = code


class FirebaseSender:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._app: Any = None

    # 이벤트 ID를 플랫폼별 알림 식별자로 사용하고 SDK 실패를 단말 폐기·영구 실패·재시도로 분류한다.
    def _send(self, delivery: dict[str, Any]) -> None:
        # 푸시를 사용하지 않는 배포는 Firebase 초기화가 필요 없으므로 발송 시점에만 불러온다.
        import firebase_admin
        from firebase_admin import credentials, exceptions, messaging

        try:
            if self._app is None:
                self._app = firebase_admin.initialize_app(
                    credentials.Certificate(self.settings.firebase_credentials_file),
                    options={
                        "projectId": self.settings.firebase_project_id,
                        "httpTimeout": 10,
                    },
                    name=f"ai-cctv-{uuid.uuid4().hex}",
                )
            event_id = str(delivery["event_id"])
            message = messaging.Message(
                token=delivery["token"],
                notification=messaging.Notification(
                    title="AI CCTV",
                    body="새 CCTV 이벤트가 있습니다. 앱에서 확인하세요.",
                ),
                data={
                    "event_id": event_id,
                    "user_id": str(delivery["user_id"]),
                    "device_id": delivery["device_id"],
                    "camera_id": delivery["camera_id"],
                    "occurred_at": delivery["occurred_at"],
                },
                android=messaging.AndroidConfig(
                    priority="high",
                    ttl=timedelta(hours=1),
                    notification=messaging.AndroidNotification(
                        channel_id="cctv_events",
                        tag=f"event-{event_id}",
                    ),
                ),
                apns=messaging.APNSConfig(
                    headers={
                        "apns-push-type": "alert",
                        "apns-priority": "10",
                        "apns-collapse-id": f"event-{event_id}",
                    },
                    payload=messaging.APNSPayload(aps=messaging.Aps(sound="default")),
                ),
            )
            messaging.send(message, app=self._app)
        except messaging.UnregisteredError as exc:
            # 더 이상 유효하지 않은 단말은 등록을 해제하고, 일시적인 통신 장애만 재시도한다.
            raise PushSendError("invalid_token", "UNREGISTERED") from exc
        except (
            messaging.SenderIdMismatchError,
            exceptions.InvalidArgumentError,
        ) as exc:
            raise PushSendError("permanent_failure", "INVALID_RECIPIENT") from exc
        except Exception as exc:
            # SDK 예외에 단말 토큰·인증 파일 경로가 포함될 수 있어 고정된 오류 코드만 전달한다.
            raise PushSendError("retry", "FCM_UNAVAILABLE") from exc

    # 동기 SDK 호출을 작업 스레드에 맡기고 45초 안에 결과가 없으면 재시도 대상으로 보고한다.
    async def send(self, delivery: dict[str, Any]) -> None:
        try:
            # 동기식 Firebase SDK 호출을 별도 스레드에서 실행해 다른 API 요청을 막지 않는다.
            await asyncio.wait_for(asyncio.to_thread(self._send, delivery), timeout=45)
        except TimeoutError as exc:
            raise PushSendError("retry", "FCM_TIMEOUT") from exc

    # 실제로 초기화된 Firebase 앱만 제거하여 발송을 사용하지 않은 배포도 정리할 수 있다.
    async def close(self) -> None:
        if self._app is not None:
            import firebase_admin

            firebase_admin.delete_app(self._app)
            self._app = None
