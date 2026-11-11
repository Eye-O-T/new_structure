import unittest
from unittest.mock import patch

from ai_cctv_edge.config import (
    BackupConfig,
    EdgeConfig,
    RecoveryConfig,
    RtspConfig,
    VideoConfig,
)
from ai_cctv_edge.control import LocalProfileRuntime


class StatusStore:
    def __init__(self, status):
        self.status = status

    def read(self):
        return self.status


class ControlRuntimeTests(unittest.TestCase):
    def runtime(self, mode, status):
        config = EdgeConfig(
            schema_version=1,
            device_id="edge-001",
            camera_id="cam-001",
            video=VideoConfig.from_profile("hd"),
            rtsp=RtspConfig(mode=mode),
            backup=BackupConfig(),
            recovery=RecoveryConfig(),
        )
        return LocalProfileRuntime(config, None, StatusStore(status))

    def wait(self, runtime):
        with patch("ai_cctv_edge.control.time.monotonic", side_effect=[0, 0, 2]):
            with patch("ai_cctv_edge.control.time.sleep"):
                return runtime.wait_for("hd", 3, 1)

    def status(self, **overrides):
        return {
            "profile_generation": 3,
            "state": "running",
            "current_video_profile": "hd",
            "camera_input": "online",
            "central_connection_status": "offline",
            "stream_requested": True,
            **overrides,
        }

    def test_publish_without_requested_stream_does_not_wait_for_central(self):
        runtime = self.runtime(
            "central_publish", self.status(stream_requested=False)
        )
        self.assertEqual(self.wait(runtime).status, "applied")

    def test_publish_with_requested_stream_waits_for_central_online(self):
        runtime = self.runtime("central_publish", self.status())
        self.assertEqual(self.wait(runtime).status, "timeout")

    def test_publish_with_requested_stream_applies_when_central_is_online(self):
        runtime = self.runtime(
            "central_publish",
            self.status(central_connection_status="online"),
        )
        self.assertEqual(self.wait(runtime).status, "applied")

    def test_capture_error_fails_even_when_central_is_not_required(self):
        runtime = self.runtime(
            "central_publish",
            self.status(state="error", stream_requested=False),
        )
        result = self.wait(runtime)
        self.assertEqual((result.status, result.reason_code), ("failed", "PIPELINE_START_FAILED"))


if __name__ == "__main__":
    unittest.main()
