import unittest
import sys
import types
from pathlib import Path

app_package = types.ModuleType("server.services.external.app")
app_package.__path__ = [
    str(Path(__file__).parents[1] / "app")
]
sys.modules.setdefault("server.services.external.app", app_package)
sys.modules.setdefault("httpx", types.ModuleType("httpx"))
from server.services.external.app.diagnostics import public_preprocessing_status


class PreprocessingDiagnosticsTests(unittest.TestCase):
    def test_idle_is_a_valid_preprocessing_status(self):
        result = public_preprocessing_status(
            {"status": "idle", "data_ready": True, "worker_count": 0, "workers": {}}
        )
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["worker_count"], 0)

    def test_exposes_sanitized_timing_and_device_fields(self):
        result = public_preprocessing_status(
            {
                "status": "ready",
                "data_ready": True,
                "inference_enabled": True,
                "gpu_requested": True,
                "worker_count": 1,
                "workers": {
                    "local-camera": {
                        "state": "online",
                        "model_ready": True,
                        "requested_device": "cuda:0",
                        "execution_device": "cuda:0",
                        "timings": {
                            "model_seconds": {
                                "samples": 10,
                                "last_seconds": 0.2,
                                "p50_seconds": 0.3,
                                "p95_seconds": 0.5,
                            },
                            "private_token": {"last_seconds": 99},
                        },
                        "last_error": "secret-bearing error text",
                        "last_error_code": "DETECTION_MODEL_FAILED",
                        "last_error_type": "ValueError",
                        "last_error_stage": "process",
                        "last_error_elapsed_seconds": 0.125,
                        "last_error_at": "2026-04-06T00:00:01Z",
                        "live_objects_observed_age_seconds": 4.2,
                        "live_objects_stale": True,
                    }
                },
            }
        )

        camera = result["cameras"][0]
        self.assertTrue(result["gpu_requested"])
        self.assertEqual(camera["execution_device"], "cuda:0")
        self.assertEqual(camera["timings"]["model_seconds"]["p95_seconds"], 0.5)
        self.assertEqual(camera["last_error_code"], "DETECTION_MODEL_FAILED")
        self.assertEqual(camera["last_error_type"], "ValueError")
        self.assertEqual(camera["last_error_stage"], "process")
        self.assertEqual(camera["last_error_elapsed_seconds"], 0.125)
        self.assertEqual(camera["last_error_at"], "2026-04-06T00:00:01+00:00")
        self.assertEqual(camera["live_objects_observed_age_seconds"], 4.2)
        self.assertTrue(camera["live_objects_stale"])
        self.assertNotIn("private_token", camera["timings"])
        self.assertNotIn("secret-bearing error text", repr(result))


if __name__ == "__main__":
    unittest.main()
