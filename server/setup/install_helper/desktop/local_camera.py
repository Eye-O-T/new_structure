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
        self.process.readyReadStandardError.connect(self._read_error_output)
        self.camera_name = ""
        self._error_output = ""
        self.failure_message = ""

    @staticmethod
    def ffmpeg_path():
        value = os.getenv("AI_CCTV_FFMPEG") or shutil.which("ffmpeg")
        if not value:
            raise RuntimeError("FFmpeg가 설치되어 있지 않습니다. AI_CCTV_FFMPEG를 설정하거나 PATH에 ffmpeg를 추가하세요.")
        return value

    def start(self, camera_name, stream_url, *, width=1280, height=720, fps=30):
        self.stop()
        self.camera_name = camera_name
        self._error_output = ""
        self.failure_message = ""
        ffmpeg = self.ffmpeg_path()
        # HLS는 키프레임에서 분할되므로 1초 세그먼트와 GOP를 맞춘다.
        gop = max(1, int(fps))
        args = [
            "-hide_banner", "-loglevel", "warning", "-f", "dshow",
            "-video_size", f"{width}x{height}", "-framerate", str(fps),
            "-i", f"video={camera_name}", "-an", "-c:v", "libx264",
            "-preset", "veryfast", "-tune", "zerolatency",
            # 30fps 기준 1초마다 키프레임을 만들어 HLS 경계와 맞춘다.
            "-g", str(gop), "-keyint_min", str(gop), "-sc_threshold", "0", "-bf", "0",
            "-pix_fmt", "yuv420p",
            "-f", "rtsp", "-rtsp_transport", "tcp", stream_url,
        ]
        self.process.start(ffmpeg, args)
        if not self.process.waitForStarted(3000):
            raise RuntimeError("FFmpeg를 시작하지 못했습니다.")
        self.state_changed.emit("starting")
        # Do not retain the credential-bearing URL after QProcess has received it.
        del stream_url

    def _read_error_output(self):
        output = bytes(self.process.readAllStandardError()).decode("utf-8", errors="replace")
        self._error_output = (self._error_output + output)[-8000:]

    def _failure_reason(self):
        output = self._error_output.lower()
        def with_output(message):
            detail = self._error_output.strip()
            return f"{message}\nFFmpeg stderr:\n{detail}" if detail else message

        if "could not set video options" in output or "could not run graph" in output:
            return with_output("웹캠이 요청한 영상 설정을 지원하지 않습니다. 카메라 장치와 해상도를 확인하세요.")
        if "could not find video device" in output or "error opening input" in output:
            return with_output("웹캠을 열 수 없습니다. 다른 앱에서 카메라를 사용 중인지, 장치명이 맞는지 확인하세요.")
        if "401" in output or "403" in output or "unauthorized" in output or "authentication failed" in output:
            return with_output("RTSP 인증에 실패했습니다. 카메라를 다시 등록한 뒤 송출을 시작하세요.")
        if "connection refused" in output or "no route to host" in output or "failed to connect" in output:
            return with_output("RTSP 서버에 연결할 수 없습니다. 서버 주소와 포트 8554를 확인하세요.")
        return with_output("FFmpeg 송출이 종료됐습니다. 웹캠, RTSP 서버, 송출 인증을 확인하세요.")

    def stop(self):
        if self.process.state() != QProcess.NotRunning:
            self.process.terminate()
            if not self.process.waitForFinished(2000):
                self.process.kill()
                self.process.waitForFinished(1000)
        self.state_changed.emit("stopped")

    def _finished(self, exit_code, _status):
        self._read_error_output()
        if exit_code:
            self.failure_message = self._failure_reason()
            self.state_changed.emit("failed")
        else:
            self.state_changed.emit("stopped")

    def _error(self, _error):
        self._read_error_output()
        self.failure_message = self._failure_reason()
        self.state_changed.emit("failed")

    def close(self):
        self.stop()


def list_camera_devices():
    """Return DirectShow video device names; an empty list is non-Windows/FFmpeg absent."""
    try:
        ffmpeg = LocalCameraPublisher.ffmpeg_path()
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True, text=False, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError, RuntimeError):
        return []
    stderr = (result.stderr or b"").decode("utf-8", errors="replace")
    devices = []
    for line in stderr.splitlines():
        # FFmpeg 9 may label DirectShow video devices as (none), while older
        # versions use (video). Alternative-name lines are not device names.
        match = re.search(r'"([^"]+)"\s+\((?:video|none)\)\s*$', line, re.IGNORECASE)
        if match and match.group(1) not in devices:
            devices.append(match.group(1))
    return devices


def rtsp_publish_url(host, port, camera_id, username, password):
    return "rtsp://%s:%s@%s:%s/%s" % (
        quote(username, safe=""), quote(password, safe=""), host, int(port), quote(camera_id, safe="")
    )
