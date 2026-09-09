"""네이티브 호출처럼 돌아오지 않는 감지기를 실제 spawn 자식으로 검증한다."""

import multiprocessing
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pytest

from server.services.preprocessing.processors.detection.contracts import DetectionFrame
from server.services.preprocessing.processors.detection.isolation import (
    IsolatedDetector,
)
from server.services.preprocessing.processors.detection import isolation


class DetectorFixture:
    def __init__(self, model_path, confidence, device):
        self.mode = str(model_path)
        if self.mode == "startup":
            threading.Event().wait()

    def reset(self):
        if self.mode == "reset":
            threading.Event().wait()

    def process(self, frame):
        if self.mode == "process":
            threading.Event().wait()
        if self.mode == "error":
            raise RuntimeError("private model path")
        return {
            "objects": [{"person_id": "1", "bbox": [0, 0, 16, 20], "confidence": 0.9}]
        }


REFERENCE = (
    "server.services.preprocessing.tests.test_detection_isolation:DetectorFixture"
)


def create(mode, **kwargs):
    return IsolatedDetector(
        REFERENCE,
        Path(mode),
        0.4,
        "cpu",
        timeout_seconds=0.2,
        startup_timeout_seconds=5,
        **kwargs,
    )


def frame():
    return DetectionFrame(
        camera_id="cam-001",
        tracking_session_id="a" * 32,
        observed_at=datetime.now(timezone.utc),
        image=np.zeros((20, 16, 3), dtype=np.uint8),
    )


@pytest.mark.parametrize("mode", ["process", "reset"])
def test_timeout_reaps_child_and_next_detector_can_run(mode):
    detector = create(mode)
    pid = detector._process.pid
    with pytest.raises(TimeoutError):
        detector.reset() if mode == "reset" else detector.process(frame())
    assert pid not in {child.pid for child in multiprocessing.active_children()}
    detector.close()
    replacement = create("ready")
    try:
        replacement.reset()
        assert replacement.process(frame()).objects[0].person_id == "1"
    finally:
        replacement.close()


def test_startup_cancellation_reaps_child_without_waiting_for_startup_limit():
    stop = threading.Event()
    before = {child.pid for child in multiprocessing.active_children()}
    errors = []

    def initialize():
        try:
            create("startup", stop_event=stop)
        except RuntimeError as error:
            errors.append(str(error))

    thread = threading.Thread(target=initialize)
    thread.start()
    time.sleep(0.2)
    stop.set()
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert errors == ["DETECTION_STOPPED"]
    assert {child.pid for child in multiprocessing.active_children()} <= before


def test_model_error_is_sanitized_and_child_closed():
    detector = create("error")
    with pytest.raises(RuntimeError, match="^DETECTION_MODEL_FAILED$"):
        detector.process(frame())
    assert detector._process is None


def no_pipe_reader(connection, *_):
    connection.send({"kind": "ready"})
    threading.Event().wait()


def test_timeout_includes_blocked_large_frame_send(monkeypatch):
    monkeypatch.setattr(isolation, "_serve", no_pipe_reader)
    detector = create("ready")
    pid = detector._process.pid
    large = frame().model_copy(
        update={"image": np.zeros((1080, 1920, 3), dtype=np.uint8)}
    )
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        detector.process(large)
    assert time.monotonic() - started < 4
    assert not detector._io_thread.is_alive()
    assert pid not in {child.pid for child in multiprocessing.active_children()}


def test_close_interrupts_inflight_model_and_reaps_io_thread():
    detector = create("process")
    detector.timeout_seconds = 30
    errors = []

    def run():
        try:
            detector.process(frame())
        except RuntimeError as error:
            errors.append(str(error))

    thread = threading.Thread(target=run)
    thread.start()
    time.sleep(0.1)
    detector.close()
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert errors
    assert not detector._io_thread.is_alive()
