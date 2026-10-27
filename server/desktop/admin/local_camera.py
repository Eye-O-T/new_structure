"""Windows local-camera capture and RTSP publishing through FFmpeg."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import quote

from PyQt5.QtCore import QObject, QProcess, pyqtSignal

from server.operations.compose import default_data_root
from server.operations.local_camera import (
    list_camera_devices,
    publisher_status,
    resolve_ffmpeg_executable,
    start_publisher,
    stop_publisher,
)


class _ProcessState:
    """Keep the small QProcess state contract used by the existing settings UI."""

    def __init__(self, owner):
        self.owner = owner

    def state(self):
        status = publisher_status(self.owner.data_root)
        return QProcess.Running if status["status"] == "running" else QProcess.NotRunning


class LocalCameraPublisher(QObject):
    state_changed = pyqtSignal(str)

    def __init__(self, parent=None, *, data_root: Path | None = None):
        super().__init__(parent)
        self.data_root = Path(data_root or default_data_root()).expanduser().resolve()
        self.process = _ProcessState(self)
        self.camera_name = ""
        self.camera_id = ""
        self._error_output = ""
        self.failure_message = ""

    @staticmethod
    def ffmpeg_path():
        value = resolve_ffmpeg_executable()
        if not value:
            raise RuntimeError(
                "FFmpeg가 설치되어 있지 않습니다. AI_CCTV_FFMPEG를 설정하거나 PATH에 ffmpeg를 추가하세요."
            )
        return value

    def start(self, camera_name, stream_url, *, camera_id="", width=1280, height=720, fps=30):
        self.camera_name = camera_name
        self.camera_id = camera_id
        self._error_output = ""
        self.failure_message = ""
        self.state_changed.emit("starting")
        try:
            start_publisher(
                self.data_root,
                device_name=camera_name,
                camera_id=camera_id,
                stream_url=stream_url,
                width=width,
                height=height,
                fps=fps,
            )
        except Exception as exc:
            self.failure_message = str(exc)
            self.state_changed.emit("failed")
            raise

    def stop(self):
        stop_publisher(self.data_root)
        self.state_changed.emit("stopped")

    def close(self):
        self.stop()


def rtsp_publish_url(host, port, camera_id, username, password):
    return "rtsp://%s:%s@%s:%s/%s" % (
        quote(username, safe=""),
        quote(password, safe=""),
        host,
        int(port),
        quote(camera_id, safe=""),
    )
