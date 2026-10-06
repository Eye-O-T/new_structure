import unittest
from email.message import Message

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


if __name__ == "__main__":
    unittest.main()
