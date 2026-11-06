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
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

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

    def _open_detail_and_collect_texts(self, window, detail_payload):
        app = QApplication.instance() or QApplication([])
        self.api.event = Mock(return_value=detail_payload)
        self.api.event_image = Mock(return_value=(b"invalid-image", "image/jpeg"))
        self.api.recording = Mock(
            return_value={
                "id": 10,
                "status": "ready",
                "start_time": "2026-11-06T06:00:00Z",
                "end_time": "2026-11-06T06:00:10Z",
            }
        )
        self.api.recording_playback = Mock(
            return_value={"playback_url": "/playback/get?path=cam-1"}
        )

        def run_exec():
            for task in list(window._event_detail_tasks):
                task.wait(2000)
            app.processEvents()
            return 0

        with patch("server.desktop.admin.application.QDialog.exec_", side_effect=run_exec):
            window.open_event_detail({"id": detail_payload["id"]})
        dialog = window._last_event_detail_dialog
        from PyQt5.QtWidgets import QLabel, QPushButton

        labels = [lbl.text() for lbl in dialog.findChildren(QLabel) if lbl.text()]
        buttons = [btn.text() for btn in dialog.findChildren(QPushButton) if btn.text()]
        dialog.close()
        dialog.deleteLater()
        return labels, buttons

    def test_person_appeared_detail_hides_analysis_and_refresh_button_even_with_legacy_metadata(self):
        QApplication.instance() or QApplication([])
        with patch(
            "server.desktop.admin.application.WindowsSessionStore", return_value=None
        ):
            window = CCTVMainWindow("https://server.example")
        try:
            window.api = self.api
            labels, buttons = self._open_detail_and_collect_texts(
                window,
                {
                    "id": 101,
                    "camera_id": "cam-1",
                    "event_type": "person_appeared",
                    "occurred_at": "2026-11-06T06:00:00Z",
                    "person_id": "7",
                    "global_person_id": 42,
                    "confidence": 0.95,
                    "media": {"snapshot": True, "crop": False, "annotated_snapshot": False},
                    "recording_segment_ids": [10],
                    "metadata": {
                        "analysis": {
                            "status": "complete",
                            "updated_at": "2026-11-06T06:00:03Z",
                            "result": {
                                "backend": "local_appearance",
                                "clothing_colors": {
                                    "upper": {
                                        "dominant_color": {
                                            "name": "blue",
                                            "rgb": [10, 20, 200],
                                            "confidence": 0.9,
                                        }
                                    }
                                },
                            },
                        }
                    },
                },
            )
            joined = "\n".join(labels)
            self.assertNotIn("분석 결과", labels)
            self.assertNotIn("상의 후보", joined)
            self.assertNotIn("분석 결과 새로고침", buttons)
            self.assertIn("이벤트 유형: person_appeared", labels)
            self.assertIn("pid: 7", labels)
            self.assertIn("gpid: 42", labels)
            self.assertIn("녹화 재생", buttons)
        finally:
            window.close()
            window.deleteLater()

    def test_analysis_completed_detail_renders_appearance_and_par_results_without_refresh_button(self):
        QApplication.instance() or QApplication([])
        with patch(
            "server.desktop.admin.application.WindowsSessionStore", return_value=None
        ):
            window = CCTVMainWindow("https://server.example")
        try:
            window.api = self.api
            # 1. LocalAppearanceAnalyzer result
            labels_local, buttons_local = self._open_detail_and_collect_texts(
                window,
                {
                    "id": 202,
                    "camera_id": "cam-1",
                    "event_type": "analysis_completed",
                    "occurred_at": "2026-11-06T06:00:03Z",
                    "person_id": "7",
                    "global_person_id": 42,
                    "media": {"snapshot": False, "crop": False, "annotated_snapshot": False},
                    "recording_segment_ids": [],
                    "metadata": {
                        "appearance_event_id": 101,
                        "appearance_occurred_at": "2026-11-06T06:00:00Z",
                        "analysis_job_id": 55,
                        "tracking_session_id": "a" * 32,
                        "analysis": {
                            "status": "complete",
                            "updated_at": "2026-11-06T06:00:03Z",
                            "result": {
                                "backend": "local_appearance",
                                "backend_version": "1.0.0",
                                "image": {
                                    "width": 120,
                                    "height": 300,
                                    "sample_width": 102,
                                    "sample_height": 256,
                                    "quality": {
                                        "luminance_mean": 120.5,
                                        "flags": ["low_contrast"],
                                    },
                                },
                                "clothing_colors": {
                                    "upper": {
                                        "dominant_color": {
                                            "name": "blue",
                                            "rgb": [10, 20, 200],
                                            "confidence": 0.8765,
                                        }
                                    },
                                    "lower": {
                                        "dominant_color": {
                                            "name": "black",
                                            "rgb": [15, 15, 15],
                                            "confidence": 0.9123,
                                        }
                                    },
                                },
                            },
                        },
                    },
                },
            )
            joined_local = "\n".join(labels_local)
            self.assertIn("분석 결과", labels_local)
            self.assertNotIn("분석 결과 새로고침", buttons_local)
            self.assertIn("원본 등장 이벤트 ID: 101", labels_local)
            self.assertIn("원본 등장 시각: 2026-11-06 15:00:00 KST", labels_local)
            self.assertIn("완료 시각: 2026-11-06 15:00:03 KST", labels_local)
            self.assertIn("pid: 7", labels_local)
            self.assertIn("gpid: 42", labels_local)
            self.assertIn("상의 후보: 파랑 · RGB 10, 20, 200 · 신뢰 지표 0.8765", labels_local)
            self.assertIn("하의 후보: 검정 · RGB 15, 15, 15 · 신뢰 지표 0.9123", labels_local)
            self.assertIn("밝기 평균: 120.5000", joined_local)
            self.assertIn("품질 표시: 대비 낮음", joined_local)

            # 2. PersonAttributeAnalyzer (PAR) result
            labels_par, buttons_par = self._open_detail_and_collect_texts(
                window,
                {
                    "id": 203,
                    "camera_id": "cam-1",
                    "event_type": "analysis_completed",
                    "occurred_at": "2026-11-06T06:00:04Z",
                    "person_id": "8",
                    "global_person_id": None,
                    "media": {"snapshot": False, "crop": False, "annotated_snapshot": False},
                    "recording_segment_ids": [],
                    "metadata": {
                        "appearance_event_id": 102,
                        "appearance_occurred_at": "2026-11-06T06:00:01Z",
                        "analysis": {
                            "status": "complete",
                            "updated_at": "2026-11-06T06:00:04Z",
                            "result": {
                                "backend": "par_resnet34",
                                "backend_version": "exp3",
                                "image": {"width": 128, "height": 256},
                                "attributes": {
                                    "hat": {"value": "Hat", "score": 0.9123},
                                    "upper_color": [
                                        {"value": "Black", "score": 0.85},
                                        {"value": "Blue", "score": 0.62},
                                    ],
                                },
                            },
                        },
                    },
                },
            )
            joined_par = "\n".join(labels_par)
            self.assertIn("분석 결과", labels_par)
            self.assertNotIn("분석 결과 새로고침", buttons_par)
            self.assertIn("모자(hat): Hat (0.9123)", labels_par)
            self.assertIn("상의 색상(upper_color): Black (0.8500), Blue (0.6200)", labels_par)
            self.assertIn("이미지 크기(원본 W×H): 128 × 256", labels_par)
            self.assertNotIn("상의 후보", joined_par)
        finally:
            window.close()
            window.deleteLater()


if __name__ == "__main__":
    unittest.main()
