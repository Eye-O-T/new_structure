import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server.services.preprocessing.app.pipeline import CameraWorker
from server.services.preprocessing.app.settings import Settings
from server.services.preprocessing.app.supervisor import DetectionSupervisor
from server.services.preprocessing.processors.detection.isolation import (
    DetectionProcessError,
    _serve,
)


class _FakePipeConnection:
    def __init__(self, incoming):
        self._incoming = list(incoming)
        self.sent = []
        self.closed = False

    def recv(self):
        if not self._incoming:
            raise EOFError()
        return self._incoming.pop(0)

    def send(self, payload):
        self.sent.append(payload)

    def close(self):
        self.closed = True


class DetectionIsolationDiagnosticsTests(unittest.TestCase):
    def test_child_reports_ready_only_after_warmup(self):
        calls = []

        class Tracker:
            def warmup(self):
                calls.append("warmup")

            def reset(self):
                pass

            def process(self, _frame):
                pass

        class Connection(_FakePipeConnection):
            def send(self, payload):
                calls.append(payload["kind"])
                super().send(payload)

        conn = Connection([])
        with patch(
            "server.services.preprocessing.processors.detection.isolation.load_factory",
            return_value=lambda *_args: Tracker(),
        ):
            _serve(conn, "pkg:Tracker", ("/model.pt", 0.35, "cpu"))
        self.assertEqual(calls, ["warmup", "ready"])

    def test_child_reports_warmup_failure_as_startup_failure(self):
        class Tracker:
            def warmup(self):
                raise RuntimeError("warmup failed")

            def reset(self):
                pass

            def process(self, _frame):
                pass

        conn = _FakePipeConnection([])
        with patch(
            "server.services.preprocessing.processors.detection.isolation.load_factory",
            return_value=lambda *_args: Tracker(),
        ):
            _serve(conn, "pkg:Tracker", ("/model.pt", 0.35, "cpu"))
        self.assertEqual(conn.sent[0]["kind"], "startup_error")

    def test_cuda_warmup_memory_failure_is_reported(self):
        class Tracker:
            def warmup(self):
                raise RuntimeError("CUDA out of memory")

            def reset(self):
                pass

            def process(self, _frame):
                pass

        conn = _FakePipeConnection([])
        with patch(
            "server.services.preprocessing.processors.detection.isolation.load_factory",
            return_value=lambda *_args: Tracker(),
        ):
            _serve(conn, "pkg:Tracker", ("/model.pt", 0.35, "cuda:0"))
        self.assertEqual(conn.sent[0]["error_code"], "GPU_OUT_OF_MEMORY")

    def _make_settings(self, snapshots_root: Path) -> Settings:
        return Settings(
            data_service_url="http://data:8000",
            rtsp_base_url="rtsp://mediamtx:8554",
            media_read_username="reader",
            media_read_password="p" * 24,
            internal_service_token="t" * 32,
            identity_token="i" * 32,
            snapshots_root=snapshots_root,
            model_path=snapshots_root / "yolo26x.pt",
            confidence=0.35,
            device="cpu",
            analysis_fps=2.0,
            disappear_seconds=4.0,
            refresh_seconds=5.0,
            capture_timeout_seconds=2.0,
            model_retry_seconds=10.0,
            shutdown_timeout_seconds=2.0,
            inference_enabled=True,
            detection_plugin="pkg:Tracker",
            detection_timeout_seconds=1.0,
            detection_startup_seconds=1.0,
            identity_plugin="pkg:Identity",
            observation_window_seconds=2.0,
            observation_buffer_max_bytes=1024 * 1024,
            event_outbox_max_pending=10,
            event_outbox_max_bytes=1024 * 1024,
        )

    def test_child_serve_preserves_error_type_stage_and_elapsed_without_leaking_message(self):
        class FailingTracker:
            def reset(self):
                raise RuntimeError("reset failed for /secret/path/model.pt")

            def process(self, _frame):
                raise ValueError("CUDA out of memory at rtsp://user:secret@camera/live")

        conn = _FakePipeConnection([("reset", None), ("process", {"image": 1})])
        with patch(
            "server.services.preprocessing.processors.detection.isolation.load_factory",
            return_value=lambda *_args: FailingTracker(),
        ):
            _serve(conn, "pkg:FailingTracker", ("/secret/model.pt", 0.35, "cpu"))

        self.assertTrue(conn.closed)
        self.assertEqual(conn.sent[0]["kind"], "ready")
        reset_err = conn.sent[1]
        self.assertEqual(reset_err["kind"], "error")
        self.assertEqual(reset_err["error_code"], "DETECTION_RESET_FAILED")
        self.assertEqual(reset_err["error_type"], "RuntimeError")
        self.assertEqual(reset_err["stage"], "reset")
        self.assertIsInstance(reset_err["elapsed_seconds"], float)
        self.assertNotIn("/secret/path", repr(reset_err))

        process_err = conn.sent[2]
        self.assertEqual(process_err["kind"], "error")
        self.assertEqual(process_err["error_code"], "GPU_OUT_OF_MEMORY")
        self.assertEqual(process_err["error_type"], "ValueError")
        self.assertEqual(process_err["stage"], "process")
        self.assertIsInstance(process_err["elapsed_seconds"], float)
        self.assertNotIn("secret", repr(process_err))

    def test_camera_worker_records_structured_model_failure_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._make_settings(Path(tmp))
            data_client = SimpleNamespace(
                set_camera_status=lambda *_args, **_kwargs: None,
                create_event=lambda *_args, **_kwargs: None,
                close=lambda: None,
            )

            def failing_factory(*_args):
                raise DetectionProcessError(
                    "DETECTION_VALIDATION_FAILED",
                    error_type="ValidationError",
                    stage="process",
                    elapsed_seconds=0.042,
                )

            worker = CameraWorker(
                {"camera_id": "local-camera", "stream_path": "local-camera"},
                settings,
                data_client,
                tracker_factory=failing_factory,
            )
            with self.assertLogs("ai_cctv.preprocessing", level="WARNING") as captured:
                tracker = worker._prepare_tracker()
            self.assertIsNone(tracker)
            snapshot = worker.status_snapshot()
            self.assertFalse(snapshot["model_ready"])
            self.assertEqual(snapshot["last_error_code"], "DETECTION_VALIDATION_FAILED")
            self.assertEqual(snapshot["last_error_type"], "ValidationError")
            self.assertEqual(snapshot["last_error_stage"], "process")
            self.assertAlmostEqual(snapshot["last_error_elapsed_seconds"], 0.042, places=3)
            self.assertIsNotNone(snapshot["last_error_at"])
            self.assertEqual(captured.records[0].error_code, "DETECTION_VALIDATION_FAILED")
            self.assertEqual(captured.records[0].error_type, "ValidationError")
            self.assertEqual(captured.records[0].stage, "process")
            self.assertIn("camera_id=local-camera", captured.output[0])
            self.assertIn("error_code=DETECTION_VALIDATION_FAILED", captured.output[0])
            self.assertIn("error_type=ValidationError", captured.output[0])
            self.assertIn("stage=process", captured.output[0])
            self.assertIn("elapsed_seconds=0.042", captured.output[0])

    def test_timeout_log_includes_measured_elapsed_and_redacts_exception_message(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._make_settings(Path(tmp))
            data_client = SimpleNamespace(
                set_camera_status=lambda *_args, **_kwargs: None,
                create_event=lambda *_args, **_kwargs: None,
                close=lambda: None,
            )

            def failing_factory(*_args):
                raise TimeoutError("timed out at /secret/path/model.pt")

            worker = CameraWorker(
                {"camera_id": "timeout-camera", "stream_path": "timeout-camera"},
                settings,
                data_client,
                tracker_factory=failing_factory,
            )
            with self.assertLogs("ai_cctv.preprocessing", level="WARNING") as captured:
                worker._prepare_tracker()

            message = captured.output[0]
            self.assertIn("camera_id=timeout-camera", message)
            self.assertIn("error_code=DETECTION_MODEL_TIMEOUT", message)
            self.assertIn("error_type=TimeoutError", message)
            self.assertIn("stage=startup", message)
            self.assertRegex(message, r"elapsed_seconds=\d+\.\d{3}")
            self.assertNotIn("/secret/path", message)

    def test_supervisor_preserves_stopped_worker_diagnostics(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = self._make_settings(Path(tmp))
            data_client = SimpleNamespace(
                enabled_cameras=lambda: [],
                set_camera_status=lambda *_args, **_kwargs: None,
                create_event=lambda *_args, **_kwargs: None,
                close=lambda: None,
            )
            supervisor = DetectionSupervisor(settings, data_client)
            try:
                dead_worker = CameraWorker(
                    {"camera_id": "local-camera", "stream_path": "local-camera"},
                    settings,
                    data_client,
                )
                dead_worker.status.last_error = "inference unavailable: DETECTION_MODEL_FAILED"
                dead_worker.status.last_error_code = "DETECTION_MODEL_FAILED"
                dead_worker.status.last_error_type = "ValueError"
                dead_worker.status.last_error_stage = "process"
                dead_worker.status.last_error_elapsed_seconds = 0.25
                supervisor._workers["local-camera"] = dead_worker
                supervisor._reconcile([])
                status = supervisor.status()
                self.assertEqual(status["workers"], {})
                self.assertIn("local-camera", status["stopped_workers"])
                stopped = status["stopped_workers"]["local-camera"]
                self.assertEqual(stopped["last_error_code"], "DETECTION_MODEL_FAILED")
                self.assertEqual(stopped["last_error_type"], "ValueError")
                self.assertEqual(stopped["last_error_stage"], "process")
            finally:
                supervisor.stop()


if __name__ == "__main__":
    unittest.main()
