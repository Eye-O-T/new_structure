"""카메라 영상을 계속 읽고 최신 프레임 한 장만 대기시킨다."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ai_cctv_core.time import format_utc, utc_now

from .performance import TimingWindow


@dataclass(frozen=True)
class CapturedFrame:
    image: Any
    sequence: int
    generation: int
    observed_at: datetime
    received_monotonic: float


@dataclass(frozen=True)
class CaptureSnapshot:
    frame: CapturedFrame | None
    connected: bool
    generation: int
    failures: int
    revision: int
    last_error_code: str | None
    last_frame_at: str | None
    last_frame_monotonic: float | None
    replaced_frames: int
    timings: dict[str, dict[str, float | int]]


class LatestFrameCapture:
    """RTSP reader with a one-frame latest-value slot and a bounded read timeout."""

    def __init__(self, source: str, timeout_seconds: float, stop_event: threading.Event):
        self._source = source
        self._timeout_seconds = timeout_seconds
        self._stop = stop_event
        self._condition = threading.Condition()
        self._frame: CapturedFrame | None = None
        self._connected = False
        self._generation = 0
        self._failures = 0
        self._revision = 0
        self._last_error_code = None
        self._last_frame_at = None
        self._last_frame_monotonic = None
        self._sequence = 0
        self._replaced_frames = 0
        self._timings = TimingWindow()
        self._thread = threading.Thread(
            target=self._run, name="camera-capture", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def snapshot(
        self, sequence: int, revision: int, timeout: float
    ) -> CaptureSnapshot:
        with self._condition:
            self._condition.wait_for(
                lambda: self._sequence > sequence
                or self._revision > revision
                or self._stop.is_set(),
                timeout=max(0, timeout),
            )
            return CaptureSnapshot(
                frame=self._frame if self._sequence > sequence else None,
                connected=self._connected,
                generation=self._generation,
                failures=self._failures,
                revision=self._revision,
                last_error_code=self._last_error_code,
                last_frame_at=self._last_frame_at,
                last_frame_monotonic=self._last_frame_monotonic,
                replaced_frames=self._replaced_frames,
                timings=self._timings.snapshot(),
            )

    def current(self) -> CaptureSnapshot:
        with self._condition:
            return CaptureSnapshot(
                frame=None,
                connected=self._connected,
                generation=self._generation,
                failures=self._failures,
                revision=self._revision,
                last_error_code=self._last_error_code,
                last_frame_at=self._last_frame_at,
                last_frame_monotonic=self._last_frame_monotonic,
                replaced_frames=self._replaced_frames,
                timings=self._timings.snapshot(),
            )

    def join(self, timeout: float) -> None:
        if self._thread.ident is not None:
            self._thread.join(timeout=max(0, timeout))

    def _changed(self) -> None:
        self._revision += 1
        self._condition.notify_all()

    def _run(self) -> None:
        # FFmpeg's RTSP default can negotiate UDP even though MediaMTX accepts TCP only.
        options = os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS", "")
        entries = [entry for entry in options.split("|") if entry]
        transport_index = next(
            (i for i, entry in enumerate(entries) if entry.split(";", 1)[0] == "rtsp_transport"),
            None,
        )
        if transport_index is None:
            entries.append("rtsp_transport;tcp")
        else:
            entries[transport_index] = "rtsp_transport;tcp"
        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "|".join(entries)

        import cv2

        timeout_ms = max(1, int(self._timeout_seconds * 1000))
        parameters = [
            cv2.CAP_PROP_OPEN_TIMEOUT_MSEC,
            timeout_ms,
            cv2.CAP_PROP_READ_TIMEOUT_MSEC,
            timeout_ms,
        ]
        delay = 1.0
        while not self._stop.is_set():
            with self._condition:
                self._generation += 1
                generation = self._generation
            capture = None
            connected_in_attempt = False
            try:
                capture = cv2.VideoCapture(
                    self._source, cv2.CAP_FFMPEG, parameters
                )
                if not capture.isOpened():
                    raise RuntimeError("RTSP_OPEN_FAILED")
                while not self._stop.is_set():
                    read_started = time.monotonic()
                    ok, frame = capture.read()
                    self._timings.record(
                        "capture_read_seconds", time.monotonic() - read_started
                    )
                    if not ok or frame is None:
                        raise RuntimeError("RTSP_READ_FAILED")
                    received = time.monotonic()
                    observed_at = utc_now()
                    with self._condition:
                        self._sequence += 1
                        if self._frame is not None:
                            self._replaced_frames += 1
                        self._frame = CapturedFrame(
                            frame,
                            self._sequence,
                            generation,
                            observed_at,
                            received,
                        )
                        self._last_frame_at = format_utc(observed_at)
                        self._last_frame_monotonic = received
                        if not connected_in_attempt:
                            self._connected = True
                            self._last_error_code = None
                            self._changed()
                            connected_in_attempt = True
                        else:
                            self._condition.notify_all()
                    delay = 1.0
            except Exception as exc:
                if not self._stop.is_set():
                    with self._condition:
                        self._connected = False
                        self._failures += 1
                        self._last_error_code = type(exc).__name__
                        self._frame = None
                        self._changed()
            finally:
                if capture is not None:
                    capture.release()
            if not self._stop.is_set():
                self._stop.wait(delay)
                delay = min(delay * 2, 15.0)
        with self._condition:
            self._connected = False
            self._frame = None
            self._changed()
