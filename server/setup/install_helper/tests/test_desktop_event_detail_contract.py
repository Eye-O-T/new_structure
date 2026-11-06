import io
import os
import unittest
from email.message import Message
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication

from server.desktop.admin.api import DesktopApi
from server.desktop.admin.application import CCTVMainWindow
from server.desktop.admin.media import PlaybackBridge


class _Response:
    def __init__(self, content_type="image/jpeg", body=b"image"):
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self._body


class DesktopEventDetailContractTests(unittest.TestCase):
    def setUp(self):
        self.api = DesktopApi("https://server.example", opener=lambda *_args, **_kwargs: _Response())
        self.api._access_token = "access"

    def test_event_and_recording_helpers_use_server_ids(self):
        calls = []
        self.api._request = lambda method, path, payload=None, **_kwargs: calls.append((method, path)) or {}

        self.api.event("event/1")
        self.api.recording("segment/1")
        self.api.recording_playback("segment/1")

        self.assertEqual(
            calls,
            [
                ("GET", "/api/v1/events/event%2F1"),
                ("GET", "/api/v1/recordings/segment%2F1"),
                ("GET", "/api/v1/recordings/segment%2F1/playback"),
            ],
        )

    def test_event_image_validates_kind_and_content_type(self):
        self.assertEqual(self.api.event_image("event-1", "snapshot"), (b"image", "image/jpeg"))
        with self.assertRaises(ValueError):
            self.api.event_image("event-1", "filesystem-path")

        self.api._opener = lambda *_args, **_kwargs: _Response("application/json")
        with self.assertRaises(ValueError):
            self.api.event_image("event-1", "crop")

    def test_event_image_rejects_oversized_response(self):
        self.api._opener = lambda *_args, **_kwargs: _Response(body=b"x" * (32 * 1024 * 1024 + 1))
        with self.assertRaises(ValueError):
            self.api.event_image("event-1", "snapshot")

    def test_event_image_refreshes_once_after_unauthorized(self):
        calls = []

        def opener(*_args, **_kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise HTTPError("https://server.example", 401, "Unauthorized", None, None)
            return _Response()

        self.api._opener = opener
        self.api._refresh = lambda: setattr(self.api, "_access_token", "refreshed")
        self.assertEqual(self.api.event_image("event-1", "snapshot"), (b"image", "image/jpeg"))
        self.assertEqual(len(calls), 2)

    def test_event_image_propagates_second_unauthorized_response(self):
        self.api._opener = lambda *_args, **_kwargs: (_ for _ in ()).throw(
            HTTPError("https://server.example", 401, "Unauthorized", None, None)
        )
        self.api._refresh = lambda: setattr(self.api, "_access_token", "refreshed")
        with self.assertRaises(HTTPError):
            self.api.event_image("event-1", "snapshot")

    def test_playback_url_resolves_same_origin_only(self):
        self.assertEqual(
            self.api.playback_url("/playback/get?path=cam-1"),
            "https://server.example/playback/get?path=cam-1",
        )
        self.assertEqual(
            self.api.playback_url("https://server.example/playback/get?path=cam-1"),
            "https://server.example/playback/get?path=cam-1",
        )
        for invalid in (
            "https://attacker.example/playback/get",
            "javascript:alert(1)",
            "//attacker.example/playback/get",
            "relative/path",
        ):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.api.playback_url(invalid)

    def test_playback_bridge_relays_authenticated_stream_and_range_headers(self):
        captured = []

        class _StreamResponse:
            def __init__(self):
                self.status = 206
                self.headers = Message()
                self.headers["Content-Type"] = "video/mp4"
                self.headers["Content-Range"] = "bytes 0-3/8"
                self.headers["Accept-Ranges"] = "bytes"
                self._chunks = [b"ftyp", b""]

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _size=-1):
                return self._chunks.pop(0) if self._chunks else b""

        def opener(request, timeout=None):
            captured.append((request, timeout))
            return _StreamResponse()

        self.api._opener = opener
        self.api._access_token = "desktop-bearer-token"

        bridge = PlaybackBridge(
            self.api, "https://server.example/playback/get?path=cam-1&duration=10"
        )
        try:
            raw_request = (
                f"GET {bridge.local_path} HTTP/1.1\r\n"
                "Host: 127.0.0.1\r\n"
                "Connection: close\r\n"
                "Range: bytes=0-3\r\n\r\n"
            ).encode("ascii")
            output = io.BytesIO()

            class _InMemorySocket:
                def makefile(self, mode, *_args, **_kwargs):
                    if "r" in mode:
                        return io.BytesIO(raw_request)
                    return output

                def sendall(self, data):
                    output.write(data)

            bridge.server.RequestHandlerClass(
                _InMemorySocket(), ("127.0.0.1", 12345), bridge.server
            )
            response_text = output.getvalue().decode("latin-1")
        finally:
            bridge.close()

        self.assertIn("206", response_text.splitlines()[0])
        self.assertIn("Content-Range: bytes 0-3/8", response_text)
        self.assertTrue(response_text.endswith("\r\n\r\nftyp"))
        self.assertEqual(len(captured), 1)
        upstream_request, _ = captured[0]
        self.assertEqual(
            upstream_request.full_url,
            "https://server.example/playback/get?path=cam-1&duration=10",
        )
        self.assertEqual(
            upstream_request.get_header("Authorization"),
            "Bearer desktop-bearer-token",
        )
        self.assertEqual(upstream_request.get_header("Range"), "bytes=0-3")

    def test_open_playback_url_uses_loopback_bridge_and_close_waits_for_detail_tasks(self):
        app = QApplication.instance() or QApplication([])
        with patch(
            "server.desktop.admin.application.WindowsSessionStore", return_value=None
        ):
            window = CCTVMainWindow("https://server.example")
        try:
            window.api = self.api
            opened_urls = []
            with patch(
                "server.desktop.admin.application.QDesktopServices.openUrl",
                side_effect=lambda qurl: opened_urls.append(qurl.toString()) or True,
            ):
                window._open_playback_url(
                    "https://server.example/api/v1/recordings/seg-1/content"
                )
            self.assertEqual(len(opened_urls), 1)
            self.assertTrue(opened_urls[0].startswith("http://127.0.0.1:"))
            self.assertEqual(len(window._playback_bridges), 1)

            pending_task = Mock()
            window._event_detail_tasks.add(pending_task)
            self.assertFalse(window.close())
            self.assertTrue(window._closing)
            self.assertEqual(len(window._playback_bridges), 0)

            window._finish_event_detail_task(pending_task)
            app.processEvents()
            self.assertEqual(len(window._event_detail_tasks), 0)
        finally:
            window._event_detail_tasks.clear()
            window.close_playback_bridges()
            window.close()
            window.deleteLater()


if __name__ == "__main__":
    unittest.main()
