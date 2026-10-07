import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import HTTPException

from server.services.external.app.api.media_auth import internal_media_auth
from server.services.external.app.schemas import MediaAuthRequest


class MediaAuthDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.lock = asyncio.Lock()
        state = SimpleNamespace(
            camera_admission_lock_factory=lambda _camera_id: self.lock
        )
        self.request = SimpleNamespace(app=SimpleNamespace(state=state))
        self.settings = SimpleNamespace(
            media_read_username="reader-user",
            media_read_password="reader-secret",
            media_publish_credentials={},
        )
        self.data = SimpleNamespace(
            get_camera=AsyncMock(return_value={"enabled": True})
        )

    async def test_success_logs_each_rtsp_auth_stage_without_credentials(self):
        payload = MediaAuthRequest(
            action="read",
            protocol="rtsp",
            path="cam-001",
            user="reader-user",
            password="reader-secret",
        )

        with self.assertLogs("ai_cctv.external.media_auth", level="INFO") as logs:
            response = await internal_media_auth(
                self.request,
                payload,
                self.settings,
                self.data,
            )

            messages = []

            async def receive():
                return {"type": "http.disconnect"}

            async def send(message):
                messages.append(message)

            await response({"type": "http"}, receive, send)

        output = "\n".join(logs.output)
        for event in (
            "MEDIA_AUTH_RECEIVED",
            "MEDIA_AUTH_LOCK_ACQUIRED",
            "MEDIA_AUTH_CAMERA_LOOKUP_COMPLETE",
            "MEDIA_AUTH_ACCEPTED",
            "MEDIA_AUTH_RESPONSE_SENT",
            "MEDIA_AUTH_LOCK_RELEASED",
        ):
            self.assertIn(event, output)
        self.assertNotIn("reader-user", output)
        self.assertNotIn("reader-secret", output)
        self.assertFalse(self.lock.locked())
        self.assertEqual(messages[0]["status"], 204)

    async def test_credential_rejection_logs_reason_without_credentials(self):
        payload = MediaAuthRequest(
            action="read",
            protocol="rtsp",
            path="cam-001",
            user="reader-user",
            password="wrong-secret",
        )

        with self.assertLogs("ai_cctv.external.media_auth", level="INFO") as logs:
            with self.assertRaises(HTTPException):
                await internal_media_auth(
                    self.request,
                    payload,
                    self.settings,
                    self.data,
                )

        output = "\n".join(logs.output)
        self.assertIn("reason=credential_mismatch", output)
        self.assertIn("MEDIA_AUTH_ABORTED", output)
        self.assertNotIn("reader-user", output)
        self.assertNotIn("wrong-secret", output)
        self.assertFalse(self.lock.locked())


if __name__ == "__main__":
    unittest.main()
