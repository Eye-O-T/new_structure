"""Optional Firebase adapter; never log recipient tokens or SDK exception text."""

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

    def _send(self, delivery: dict[str, Any]) -> None:
        # Optional at runtime: deployments without push need no Firebase setup.
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
            raise PushSendError("invalid_token", "UNREGISTERED") from exc
        except (
            messaging.SenderIdMismatchError,
            exceptions.InvalidArgumentError,
        ) as exc:
            raise PushSendError("permanent_failure", "INVALID_RECIPIENT") from exc
        except Exception as exc:
            # SDK exceptions can contain tokens/credential paths; never log them.
            raise PushSendError("retry", "FCM_UNAVAILABLE") from exc

    async def send(self, delivery: dict[str, Any]) -> None:
        try:
            await asyncio.wait_for(asyncio.to_thread(self._send, delivery), timeout=45)
        except TimeoutError as exc:
            raise PushSendError("retry", "FCM_TIMEOUT") from exc

    async def close(self) -> None:
        if self._app is not None:
            import firebase_admin

            firebase_admin.delete_app(self._app)
            self._app = None
