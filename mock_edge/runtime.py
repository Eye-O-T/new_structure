from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable
from urllib.parse import quote


LOGGER = logging.getLogger("ai_cctv.mock_edge.media")


class MockEdgeRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class VideoProfile:
    name: str
    width: int
    height: int
    fps: int
    bitrate_kbps: int


VIDEO_PROFILES = {
    "hd": VideoProfile("hd", 1280, 720, 30, 2_000),
    "fhd": VideoProfile("fhd", 1920, 1080, 30, 4_000),
}


@dataclass(frozen=True, slots=True)
class CentralTarget:
    host: str
    port: int
    camera_id: str
    username: str
    password: str

    def validate(self) -> None:
        if (
            not self.host
            or self.host in {"0.0.0.0", "::"}
            or "://" in self.host
            or any(character.isspace() or character in "/@?#" for character in self.host)
        ):
            raise ValueError("central RTSP host is invalid")
        if isinstance(self.port, bool) or not 1 <= self.port <= 65_535:
            raise ValueError("central RTSP port must be in range 1..65535")
        if not self.username or any(character in "\x00\r\n" for character in self.username):
            raise ValueError("publish username is invalid")
        if len(self.password) < 16 or any(
            character in "\x00\r\n" for character in self.password
        ):
            raise ValueError("publish password must contain at least 16 characters")

    @property
    def rtsp_url(self) -> str:
        self.validate()
        username = quote(self.username, safe="")
        password = quote(self.password, safe="")
        camera_id = quote(self.camera_id, safe="-._~")
        return f"rtsp://{username}:{password}@{self.host}:{self.port}/{camera_id}"


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def resolve_ffmpeg(executable: str) -> str:
    """Resolve FFmpeg and verify the libx264 encoder used by the loop sender."""

    resolved: str | None = None
    if executable == "ffmpeg" and getattr(sys, "frozen", False):
        bundled = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        candidate = bundled / "tools" / "ffmpeg.exe"
        if candidate.is_file():
            resolved = str(candidate)
    if resolved is None:
        resolved = shutil.which(executable)
    if resolved is None:
        raise MockEdgeRuntimeError(
            f"FFmpeg 실행 파일을 찾을 수 없습니다: {executable}. "
            "FFmpeg를 설치하고 PATH에 추가하거나 --ffmpeg를 지정하세요."
        )
    creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    try:
        result = subprocess.run(
            [resolved, "-hide_banner", "-encoders"],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
            creationflags=creationflags,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise MockEdgeRuntimeError(f"FFmpeg를 실행할 수 없습니다: {exc}") from exc
    if result.returncode != 0:
        raise MockEdgeRuntimeError("FFmpeg 인코더 목록을 확인하지 못했습니다.")
    if "libx264" not in result.stdout:
        raise MockEdgeRuntimeError("FFmpeg에 필요한 libx264 인코더가 없습니다.")
    return resolved


def _common_encoding_arguments(
    video_path: Path,
    profile: VideoProfile,
) -> list[str]:
    video_filter = (
        f"scale={profile.width}:{profile.height}:force_original_aspect_ratio=decrease,"
        f"pad={profile.width}:{profile.height}:(ow-iw)/2:(oh-ih)/2,"
        f"fps={profile.fps}"
    )
    return [
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",
        "-stream_loop",
        "-1",
        "-re",
        "-i",
        str(video_path),
        "-map",
        "0:v:0",
        "-an",
        "-vf",
        video_filter,
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-tune",
        "zerolatency",
        "-b:v",
        f"{profile.bitrate_kbps}k",
        "-maxrate",
        f"{profile.bitrate_kbps}k",
        "-bufsize",
        f"{profile.bitrate_kbps * 2}k",
        "-g",
        str(profile.fps),
        "-keyint_min",
        str(profile.fps),
        "-sc_threshold",
        "0",
        "-pix_fmt",
        "yuv420p",
    ]


def build_publisher_command(
    executable: str,
    video_path: Path,
    profile: VideoProfile,
    target: CentralTarget,
) -> list[str]:
    """Build a shell-free real-time MP4 loop publishing RTSP/1.0 over TCP."""

    return [
        executable,
        *_common_encoding_arguments(video_path, profile),
        "-rtsp_transport",
        "tcp",
        "-muxdelay",
        "0.1",
        "-f",
        "rtsp",
        target.rtsp_url,
    ]


def build_recorder_command(
    executable: str,
    video_path: Path,
    profile: VideoProfile,
    backup_root: Path,
    camera_id: str,
    segment_seconds: int,
    *,
    now: datetime | None = None,
) -> tuple[list[str], Path]:
    """Build a second loop encoder that produces Edge-compatible MPEG-TS files."""

    started = (now or datetime.now(UTC)).astimezone(UTC)
    directory = (
        backup_root
        / camera_id
        / started.strftime("%Y")
        / started.strftime("%m")
        / started.strftime("%d")
    )
    directory.mkdir(parents=True, exist_ok=True)
    prefix = started.strftime("%Y%m%dT%H%M%S.%fZ")
    pattern = directory / f"{prefix}_%06d.ts"
    command = [
        executable,
        *_common_encoding_arguments(video_path, profile),
        "-f",
        "segment",
        "-segment_time",
        str(segment_seconds),
        "-reset_timestamps",
        "1",
        "-segment_format",
        "mpegts",
        str(pattern),
    ]
    return command, pattern


class ManagedProcess:
    def __init__(self, name: str) -> None:
        self.name = name
        self._process: subprocess.Popen[str] | None = None
        self._stderr_thread: threading.Thread | None = None
        self._stderr_lines: deque[str] = deque(maxlen=30)
        self._secrets: tuple[str, ...] = ()

    def start(self, command: list[str], *, secrets: tuple[str, ...] = ()) -> None:
        if self.running:
            raise MockEdgeRuntimeError(f"{self.name} process is already running")
        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self._stderr_lines.clear()
        self._secrets = tuple(secret for secret in secrets if secret)
        try:
            self._process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )
        except OSError as exc:
            self._process = None
            raise MockEdgeRuntimeError(f"{self.name} FFmpeg를 시작하지 못했습니다: {exc}") from exc
        self._stderr_thread = threading.Thread(
            target=self._consume_stderr,
            name=f"mock-edge-{self.name}-stderr",
            daemon=True,
        )
        self._stderr_thread.start()

    def _redact(self, value: str) -> str:
        result = value
        for secret in self._secrets:
            result = result.replace(secret, "***")
            result = result.replace(quote(secret, safe=""), "***")
        return result

    def _consume_stderr(self) -> None:
        process = self._process
        if process is None or process.stderr is None:
            return
        for line in process.stderr:
            message = self._redact(line.rstrip())
            if message:
                self._stderr_lines.append(message)
                LOGGER.warning("%s FFmpeg: %s", self.name, message)

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def exit_code(self) -> int | None:
        return None if self._process is None else self._process.poll()

    @property
    def last_error(self) -> str | None:
        return self._stderr_lines[-1] if self._stderr_lines else None

    def stop(self) -> None:
        process = self._process
        self._process = None
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        if self._stderr_thread is not None:
            self._stderr_thread.join(timeout=1)
            self._stderr_thread = None


class MediaEngine:
    """Own looped RTSP publication and independent local MPEG-TS recording."""

    def __init__(
        self,
        *,
        video_path: Path,
        backup_root: Path,
        camera_id: str,
        ffmpeg: str,
        segment_seconds: int,
        event_callback: Callable[[str, dict[str, object]], None] | None = None,
    ) -> None:
        self.video_path = video_path.resolve(strict=True)
        self.backup_root = backup_root.resolve()
        self.camera_id = camera_id
        self.ffmpeg_name = ffmpeg
        self.segment_seconds = segment_seconds
        self.event_callback = event_callback or (lambda _event, _details: None)
        self.profile = "hd"
        self.target: CentralTarget | None = None
        self.publisher = ManagedProcess("publisher")
        self.recorder = ManagedProcess("recorder")
        self._resolved_ffmpeg: str | None = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._monitor: threading.Thread | None = None
        self._started = False
        self._publisher_suspended = False
        self._publisher_confirmed = False
        self._confirm_after = 0.0
        self._outage_reported = False
        self._next_publish_attempt = 0.0
        self._last_error: str | None = None

    def configure(self, target: CentralTarget, profile: str) -> None:
        target.validate()
        if profile not in VIDEO_PROFILES:
            raise ValueError("video profile must be hd or fhd")
        with self._lock:
            self.target = target
            self.profile = profile
            if self._started:
                self._restart_media_locked()

    def _ffmpeg(self) -> str:
        if self._resolved_ffmpeg is None:
            self._resolved_ffmpeg = resolve_ffmpeg(self.ffmpeg_name)
        return self._resolved_ffmpeg

    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._stop.clear()
            if self.target is not None:
                self._start_recorder_locked()
                self._start_publisher_locked()
            self._monitor = threading.Thread(
                target=self._monitor_loop,
                name="mock-edge-media-monitor",
                daemon=True,
            )
            self._monitor.start()

    def stop(self) -> None:
        with self._lock:
            self._started = False
            self._stop.set()
            self.publisher.stop()
            self.recorder.stop()
        if self._monitor is not None:
            self._monitor.join(timeout=3)
            self._monitor = None

    def _start_recorder_locked(self) -> None:
        if self.recorder.running or self.target is None:
            return
        command, pattern = build_recorder_command(
            self._ffmpeg(),
            self.video_path,
            VIDEO_PROFILES[self.profile],
            self.backup_root,
            self.camera_id,
            self.segment_seconds,
        )
        self.recorder.start(command)
        LOGGER.info("로컬 MPEG-TS 백업 시작: %s", pattern)

    def _start_publisher_locked(self) -> None:
        if (
            self.publisher.running
            or self.target is None
            or self._publisher_suspended
        ):
            return
        command = build_publisher_command(
            self._ffmpeg(),
            self.video_path,
            VIDEO_PROFILES[self.profile],
            self.target,
        )
        self.publisher.start(command, secrets=(self.target.password,))
        self._publisher_confirmed = False
        self._confirm_after = time.monotonic() + 1.5
        LOGGER.info(
            "중앙 RTSP 게시 시작: host=%s port=%d camera_id=%s profile=%s",
            self.target.host,
            self.target.port,
            self.camera_id,
            self.profile,
        )

    def _restart_media_locked(self) -> None:
        self.publisher.stop()
        self.recorder.stop()
        self._publisher_confirmed = False
        if self.target is not None:
            self._start_recorder_locked()
            self._start_publisher_locked()

    def apply_profile(self, profile: str) -> tuple[bool, str | None]:
        if profile not in VIDEO_PROFILES:
            return False, "UNSUPPORTED_VIDEO_PROFILE"
        with self._lock:
            previous = self.profile
            if profile == previous:
                return True, None
            self.profile = profile
            try:
                if self._started and self.target is not None:
                    self._restart_media_locked()
            except Exception:
                self.profile = previous
                try:
                    if self._started and self.target is not None:
                        self._restart_media_locked()
                except Exception:
                    return False, "ROLLBACK_FAILED"
                return False, "PIPELINE_START_FAILED"
        return True, None

    def suspend_publisher(self) -> None:
        with self._lock:
            if self._publisher_suspended:
                return
            self._publisher_suspended = True
            self.publisher.stop()
            self._publisher_confirmed = False
            if not self._outage_reported:
                self.event_callback(
                    "central_connection_lost", {"reason": "mock_operator_request"}
                )
                self._outage_reported = True

    def resume_publisher(self) -> None:
        with self._lock:
            if not self._publisher_suspended:
                return
            self._publisher_suspended = False
            self._next_publish_attempt = 0.0
            if self._started and self.target is not None:
                self._start_publisher_locked()

    def _monitor_loop(self) -> None:
        while not self._stop.wait(0.5):
            with self._lock:
                if not self._started or self.target is None:
                    continue
                now = time.monotonic()
                if not self.recorder.running:
                    error = self.recorder.last_error
                    if error:
                        self._last_error = error
                    try:
                        self._start_recorder_locked()
                    except Exception as exc:
                        self._last_error = type(exc).__name__
                if self._publisher_suspended:
                    continue
                if self.publisher.running:
                    if not self._publisher_confirmed and now >= self._confirm_after:
                        self._publisher_confirmed = True
                        if self._outage_reported:
                            self.event_callback(
                                "central_connection_restored",
                                {"reason": "mock_rtsp_process_running"},
                            )
                            self._outage_reported = False
                    continue
                error = self.publisher.last_error
                if error:
                    self._last_error = error
                if not self._outage_reported:
                    self.event_callback(
                        "central_connection_lost",
                        {"reason": "mock_rtsp_process_exited"},
                    )
                    self._outage_reported = True
                if now < self._next_publish_attempt:
                    continue
                self._next_publish_attempt = now + 2.0
                try:
                    self._start_publisher_locked()
                except Exception as exc:
                    self._last_error = type(exc).__name__

    @property
    def configured(self) -> bool:
        return self.target is not None

    @property
    def recorder_running(self) -> bool:
        return self.recorder.running

    @property
    def publisher_running(self) -> bool:
        return self.publisher.running and self._publisher_confirmed

    @property
    def publisher_suspended(self) -> bool:
        return self._publisher_suspended

    @property
    def last_error(self) -> str | None:
        return self._last_error or self.publisher.last_error or self.recorder.last_error
