"""Native migration: real Qt lifecycle, API authentication and decoded HLS frames."""

import io
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import urlopen

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtGui import QFont, QFontDatabase, QImage
from PyQt5.QtTest import QTest
from PyQt5.QtWidgets import QApplication, QDialog

from server.setup.install_helper.desktop.api import DesktopApi
from server.setup.install_helper.desktop.application import CCTVMainWindow
from server.setup.install_helper.desktop.management import CameraManagement
from server.setup.install_helper.desktop.media import MediaBridge
from server.setup.install_helper.desktop.settings_window import SettingsWindow
from server.setup.install_helper.desktop.video_worker import ObservationAdapter, VideoWorker, legacy_event


@pytest.fixture(scope="module")
def app():
    value = QApplication.instance() or QApplication([])
    if not QFontDatabase().families():
        font_id = QFontDatabase.addApplicationFont("C:/Windows/Fonts/malgun.ttf")
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            value.setFont(QFont(families[0], 10))
    return value


def wait(app, predicate):
    deadline = time.monotonic() + 15
    while not predicate():
        app.processEvents()
        QTest.qWait(10)
        assert time.monotonic() < deadline
    app.processEvents()


def response(payload):
    return io.BytesIO(json.dumps(payload).encode())


def test_refresh_retries_once_and_keeps_tokens_private():
    calls = []

    def opener(request, **kwargs):
        calls.append(request)
        path = request.full_url
        if path.endswith("/login"):
            return response({"access_token": "old", "refresh_token": "refresh"})
        if path.endswith("/refresh"):
            return response({"access_token": "new", "refresh_token": "rotated"})
        if path.endswith("/logout"):
            return response({})
        if request.get_header("Authorization") == "Bearer old" and len(calls) > 2:
            raise HTTPError(path, 401, "expired", {}, io.BytesIO(b"{}"))
        return response({"status": "ready"})

    client = DesktopApi("https://example.test", opener=opener)
    result = client.login("admin", "password")
    assert result["access_token"] == "[redacted]"
    client.system_status()
    assert sum(c.full_url.endswith("/refresh") for c in calls) == 1
    assert calls[-1].get_header("Authorization") == "Bearer new"
    client.logout()
    assert client._access_token is None and client._refresh_token is None
    logout = next(request for request in calls if request.full_url.endswith("/logout"))
    assert logout.get_header("Authorization") == "Bearer new"


@pytest.mark.parametrize("reference", [
    "https://attacker.test/hls/cam-1/index.m3u8", "/hls/cam-2/index.m3u8",
    "/api/v1/admin/system/status", "/hls/cam-1/%2e%2e/key", "file:///secret",
    "https://user:password@example.test/hls/cam-1/index.m3u8",
])
def test_media_rejects_foreign_origins_paths_and_credentials(reference):
    client = DesktopApi("https://example.test")
    with pytest.raises(ValueError):
        client.media_path("cam-1", reference)


def test_playlist_rewrites_segments_and_keys_and_protects_loopback():
    class MediaApi(DesktopApi):
        def media(self, camera_id, path):
            return b'#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\nsegment.mp4\n'

    client = MediaApi("https://example.test")
    with MediaBridge(client, "cam-1", "/hls/cam-1/index.m3u8") as bridge:
        with urlopen(bridge.url) as reply:
            body = reply.read().decode()
        assert bridge.prefix + "/hls/cam-1/init.mp4" in body
        assert bridge.prefix + "/hls/cam-1/segment.mp4" in body
        with pytest.raises(HTTPError) as exc:
            urlopen(f"http://127.0.0.1:{bridge.server.server_port}/hls/cam-1/index.m3u8")
        assert exc.value.code == 404
        with pytest.raises(ValueError):
            bridge.playlist(b'#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="https://attacker.test/key"', "/hls/cam-1/index.m3u8")
    assert not bridge.thread.is_alive()


def test_observations_respect_freshness_and_tracking_sessions():
    adapter = ObservationAdapter()
    payload = {"observed_at": datetime.now(timezone.utc).isoformat(),
               "tracking_session_id": "a", "objects": [{"person_id": "1"}]}
    assert adapter.metrics(payload) == {"current_objects": 1, "tracked_total": 1}
    payload["tracking_session_id"] = "b"
    assert adapter.metrics(payload)["tracked_total"] == 2
    payload["observed_at"] = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
    assert adapter.metrics(payload)["current_objects"] == "—"
    assert legacy_event({"event_type": "person_appeared", "person_id": "1", "occurred_at": "now"})["type"] == "appear"


class FakeApi:
    base_url = "https://example.test"

    def __init__(self, *args, **kwargs):
        self.calls = []

    def login(self, username, password):
        self.calls.append((username, password))

    def logout(self):
        self.calls.append("logout")

    def cameras(self):
        return [{"camera_id": "cam-1", "name": "정문"}]

    def camera(self, camera_id, suffix="", method="GET", payload=None):
        self.calls.append((camera_id, suffix, method, payload))
        if suffix == "/video-profile" and method == "GET":
            return {"supported_profiles": ["hd", "fhd"], "current_profile": "hd", "edge_online": True}
        return {"camera_id": camera_id, "online": True}

    def system_status(self):
        return {"status": "ready"}


def test_settings_preserve_save_flow_and_never_keep_password(app, monkeypatch):
    monkeypatch.setattr("server.setup.install_helper.desktop.settings_window.DesktopApi", FakeApi)
    window = CCTVMainWindow()
    dialog = SettingsWindow(window)
    dialog.password.setText("secret-password")
    dialog.login()
    wait(app, lambda: dialog.task is None)
    assert dialog.password.text() == ""
    assert dialog.camera.currentData() == "cam-1"
    dialog.save_basic_settings()
    assert dialog.result() == QDialog.Accepted
    assert dialog.selected_source == (window.api, "cam-1")
    window.close()
    wait(app, lambda: window._logout_done)


def test_camera_management_updates_and_retains_unsaved_credentials(app, monkeypatch, tmp_path):
    api = FakeApi()
    dialog = CameraManagement(api)
    wait(app, lambda: dialog.task is None)
    dialog.inspect()
    wait(app, lambda: dialog.task is None)
    assert dialog.apply_profile.isEnabled()
    dialog.fields["name"].setText("새 이름")
    dialog.update()
    wait(app, lambda: dialog.task is None)
    assert ("cam-1", "", "PATCH", {"name": "새 이름"}) in api.calls
    pending = {"camera_id": "cam-1", "publish_credentials": {"username": "cam-1", "password": "secret"}}
    monkeypatch.setattr("server.setup.install_helper.desktop.management.QFileDialog.getSaveFileName", lambda *args: ("", ""))
    monkeypatch.setattr("server.setup.install_helper.desktop.management.write_publish_credentials", lambda *args: (_ for _ in ()).throw(OSError()))
    dialog.output_path = tmp_path / "publish.json"
    dialog.handoff(pending)
    assert dialog.pending == pending and dialog.save_button.isEnabled()
    dialog.show()
    dialog.close()
    assert dialog.isVisible()
    dialog.pending = None
    dialog.close()


def test_start_stop_uses_reference_signal_flow_and_retains_worker(app, monkeypatch):
    class Worker(QThread):
        frame_ready = pyqtSignal(object)
        metrics_ready = pyqtSignal(dict)
        event_ready = pyqtSignal(dict)
        loading_ready = pyqtSignal(str)

        def __init__(self, **kwargs):
            super().__init__()
            self.running = True

        def run(self):
            self.metrics_ready.emit({"current_objects": 2, "tracked_total": 3})
            self.event_ready.emit({"type": "appear", "person_id": "7"})
            while self.running:
                self.msleep(10)

        def stop(self):
            self.running = False

    monkeypatch.setattr("server.setup.install_helper.desktop.legacy_gui.VideoWorker", Worker)
    window = CCTVMainWindow()
    window.api = FakeApi()
    window.video_source = (window.api, "cam-1")
    window.start_video()
    wait(app, lambda: window.metric_current["value"].text() == "2")
    assert window.metric_appear["value"].text() == "1"
    window.stop_video()
    assert window.worker is not None
    wait(app, lambda: window.worker is None)
    assert window.btn_start.isEnabled()
    assert not any(isinstance(call, tuple) and "PATCH" in call for call in window.api.calls)
    window.close()
    wait(app, lambda: window._logout_done)


def test_real_hls_bridge_decodes_frame_and_worker_stops(app):
    import av

    output = io.BytesIO()
    with av.open(output, "w", format="mpegts") as container:
        stream = container.add_stream("libx264", rate=10)
        stream.width, stream.height, stream.pix_fmt = 64, 48, "yuv420p"
        for _ in range(10):
            frame = av.VideoFrame(64, 48, "yuv420p")
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    segment = output.getvalue()

    class Api(DesktopApi):
        def camera(self, camera_id, suffix="", **kwargs):
            if suffix == "/live":
                return {"hls_url": "/hls/cam-1/index.m3u8"}
            return {}

        def events(self, *args):
            return {"items": []}

        def media(self, camera_id, path):
            if path.endswith(".m3u8"):
                return b"#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:1,\nsegment.ts\n#EXT-X-ENDLIST\n"
            return segment

    worker = VideoWorker(source=(Api("https://example.test"), "cam-1"))
    frames = []

    def received(frame):
        frames.append(frame)
        worker.acknowledge_frame()
        worker.stop()

    worker.frame_ready.connect(received)
    worker.start()
    try:
        wait(app, lambda: bool(frames))
    finally:
        worker.stop()
        wait(app, lambda: not worker.isRunning())
    assert isinstance(frames[0], QImage)
    assert (frames[0].width(), frames[0].height()) == (64, 48)


def test_native_window_preview(app):
    window = CCTVMainWindow(storage_path="C:/ProgramData/AI_CCTV")
    window.show()
    app.processEvents()
    output = Path(".review/desktop-migration")
    output.mkdir(parents=True, exist_ok=True)
    assert window.grab().save(str(output / "main.png"))
    window.close()
