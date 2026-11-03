import os
import threading
import time
import types
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

core = types.ModuleType("ai_cctv_core")
core.__path__ = []
core_time = types.ModuleType("ai_cctv_core.time")
core_time.format_utc = lambda value: value.isoformat()
core_time.utc_now = lambda: datetime.now(timezone.utc)
import sys

sys.modules.setdefault("ai_cctv_core", core)
sys.modules.setdefault("ai_cctv_core.time", core_time)

from server.services.preprocessing.app.camera_capture import LatestFrameCapture
from server.services.preprocessing.app.performance import TimingWindow


class PerformanceTests(unittest.TestCase):
    def test_timing_window_reports_latest_separately_from_percentiles(self):
        timing = TimingWindow()
        for value in (3.0, 1.0, 2.0):
            timing.record("inference", value)

        result = timing.snapshot()["inference"]
        self.assertEqual(result["last_seconds"], 2.0)
        self.assertEqual(result["p50_seconds"], 2.0)
        self.assertEqual(result["p95_seconds"], 2.0)

    def test_capture_keeps_only_the_newest_frame_while_consumer_is_slow(self):
        class FakeCapture:
            def __init__(self):
                self.frame_id = 0

            def isOpened(self):
                return True

            def read(self):
                time.sleep(0.002)
                self.frame_id += 1
                return True, self.frame_id

            def release(self):
                pass

        fake_cv2 = types.SimpleNamespace(
            CAP_PROP_OPEN_TIMEOUT_MSEC=1,
            CAP_PROP_READ_TIMEOUT_MSEC=2,
            CAP_FFMPEG=3,
            VideoCapture=lambda *_args: FakeCapture(),
        )
        stop = threading.Event()
        capture = LatestFrameCapture("rtsp://camera/stream", 1, stop)
        original_options = os.environ.get("OPENCV_FFMPEG_CAPTURE_OPTIONS")
        try:
            with patch.dict("sys.modules", {"cv2": fake_cv2}):
                capture.start()
                first = capture.snapshot(0, 0, 1).frame
                self.assertIsNotNone(first)
                time.sleep(0.05)
                latest = capture.snapshot(first.sequence, 1, 1)
                self.assertGreater(latest.frame.sequence, first.sequence)
                self.assertGreater(latest.replaced_frames, 0)
        finally:
            stop.set()
            capture.join(2)
            if original_options is None:
                os.environ.pop("OPENCV_FFMPEG_CAPTURE_OPTIONS", None)
            else:
                os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = original_options


if __name__ == "__main__":
    unittest.main()
