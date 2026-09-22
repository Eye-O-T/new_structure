"""Windows local-camera capture and RTSP publishing through FFmpeg."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from urllib.parse import quote

from PyQt5.QtCore import QObject, QProcess, pyqtSignal


class LocalCameraPublisher(QObject):
    state_changed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.process = QProcess(self)
        self.process.finished.connect(self._finished)
        self.process.errorOccurred.connect(self._error)
        self.camera_name = ""

    @staticmethod
    def ffmpeg_path():
        value = os.getenv("AI_CCTV_FFMPEG") or shutil.which("ffmpeg")
        if not value:
            raise RuntimeError("FFmpeg가 설치되어 있지 않습니다. AI_CCTV_FFMPEG를 설정하거나 PATH에 ffmpeg를 추가하세요.")
        return value

    def start(self, camera_name, stream_url, *, width=1280, height=720, fps=15):
        self.stop()
        self.camera_name = camera_name
        ffmpeg = self.ffmpeg_path()
        args = [
            "-hide_banner", "-loglevel", "warning", "-f", "dshow",
            "-video_size", f"{width}x{height}", "-framerate", str(fps),
            "-i", f"video={camera_name}", "-an", "-c:v", "libx264",
            "-preset", "veryfast", "-tune", "zerolatency", "-pix_fmt", "yuv420p",
            "-f", "rtsp", "-rtsp_transport", "tcp", stream_url,
        ]
        self.process.start(ffmpeg, args)
        if not self.process.waitForStarted(3000):
            raise RuntimeError("FFmpeg를 시작하지 못했습니다.")
        self.state_changed.emit("starting")
        # Do not retain the credential-bearing URL after QProcess has received it.
        del stream_url

    def stop(self):
        if self.process.state() != QProcess.NotRunning:
            self.process.terminate()
            if not self.process.waitForFinished(2000):
                self.process.kill()
                self.process.waitForFinished(1000)
        self.state_changed.emit("stopped")

    def _finished(self, exit_code, _status):
        if exit_code:
            self.state_changed.emit("failed")
        else:
            self.state_changed.emit("stopped")

    def _error(self, _error):
        self.state_changed.emit("failed")

    def close(self):
        self.stop()


def list_camera_devices():
    """Return DirectShow video device names; an empty list is non-Windows/FFmpeg absent."""
    try:
        ffmpeg = LocalCameraPublisher.ffmpeg_path()
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError, RuntimeError):
        return []
    devices = []
    for line in (result.stderr or "").splitlines():
        match = re.search(r'"([^"]+)"\s+\(video\)', line, re.IGNORECASE)
        if match and match.group(1) not in devices:
            devices.append(match.group(1))
    return devices


def rtsp_publish_url(host, port, camera_id, username, password):
    return "rtsp://%s:%s@%s:%s/%s" % (
        quote(username, safe=""), quote(password, safe=""), host, int(port), quote(camera_id, safe="")
    )
