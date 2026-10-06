import unittest
from email.message import Message
from urllib.error import HTTPError

from server.setup.install_helper.desktop.api import DesktopApi


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
        self.api._request = lambda method, path, payload=None: calls.append((method, path)) or {}

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


if __name__ == "__main__":
    unittest.main()
