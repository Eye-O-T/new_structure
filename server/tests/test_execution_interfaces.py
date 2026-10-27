from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.desktop.launcher import desktop_command
from server.edge_onboarding.host_helper import run_host_helper
from server.local_camera_publisher.__main__ import _build_command, _sanitize_error
from server.operations.local_camera import (
    _pid_running,
    error_state_path,
    load_device_setting,
    publisher_status,
    save_device_setting,
    start_publisher,
)


class ExecutionInterfaceTests(unittest.TestCase):
    def test_host_processes_have_source_entrypoints(self):
        self.assertIn("server.host_helper", desktop_command("host_helper"))
        self.assertIn("server.local_camera_publisher", desktop_command("local_camera"))

    def test_local_camera_setting_contains_only_the_device_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            save_device_setting(root, "USB Camera")
            self.assertEqual(load_device_setting(root), "USB Camera")
            self.assertNotIn("password", (root / "config" / "local-camera.json").read_text(encoding="utf-8"))

    def test_stale_local_camera_state_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "runtime" / "local-camera.json"
            path.parent.mkdir(parents=True)
            path.write_text('{"helper_pid": 10, "ffmpeg_pid": 11}', encoding="utf-8")
            with patch("server.operations.local_camera._pid_running", return_value=False):
                self.assertEqual(publisher_status(root), {"status": "stopped"})
            self.assertFalse(path.exists())

    def test_pid_running_recognizes_current_process_without_subprocess(self):
        with patch("server.operations.local_camera.subprocess.run") as run_mock:
            self.assertTrue(_pid_running(os.getpid()))
            self.assertFalse(_pid_running(-1))
            self.assertFalse(_pid_running(99999999))
            run_mock.assert_not_called()

    def test_start_publisher_reports_helper_error_detail_with_redacted_url(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            class FakeProcess:
                def __init__(self):
                    self.stdin = type("Stdin", (), {"write": lambda *_a: None, "close": lambda *_a: None})()

                def poll(self):
                    err = error_state_path(root)
                    err.parent.mkdir(parents=True, exist_ok=True)
                    err.write_text(
                        _sanitize_error("Connection to rtsp://admin:secret@127.0.0.1:8554/local-camera failed"),
                        encoding="utf-8",
                    )
                    return 1

            with patch("server.operations.local_camera.subprocess.Popen", return_value=FakeProcess()):
                with self.assertRaises(RuntimeError) as ctx:
                    start_publisher(
                        root,
                        device_name="720p HD Camera",
                        camera_id="local-camera",
                        stream_url="rtsp://admin:secret@127.0.0.1:8554/local-camera",
                    )
            self.assertIn("rtsp://<redacted>", str(ctx.exception))
            self.assertNotIn("secret", str(ctx.exception))

    def test_dshow_command_supports_explicit_and_fallback_modes(self):
        explicit = _build_command(
            "ffmpeg",
            device="720p HD Camera",
            stream_url="rtsp://127.0.0.1:8554/local-camera",
            width=1280,
            height=720,
            fps=30,
            explicit_dshow_mode=True,
        )
        fallback = _build_command(
            "ffmpeg",
            device="720p HD Camera",
            stream_url="rtsp://127.0.0.1:8554/local-camera",
            width=1280,
            height=720,
            fps=30,
            explicit_dshow_mode=False,
        )
        self.assertIn("-video_size", explicit)
        self.assertNotIn("-vf", explicit)
        self.assertNotIn("-video_size", fallback)
        self.assertIn("-vf", fallback)

    def test_duplicate_local_camera_start_reuses_the_running_process(self):
        expected = {
            "status": "running",
            "pid": 42,
            "camera_id": "local-camera",
            "device_name": "USB Camera",
        }
        with tempfile.TemporaryDirectory() as directory, patch(
            "server.operations.local_camera.publisher_status", return_value=expected
        ):
            actual = start_publisher(
                Path(directory),
                device_name="USB Camera",
                camera_id="local-camera",
                stream_url="rtsp://user:secret@127.0.0.1:8554/local-camera",
            )
        self.assertEqual(actual, expected)

    def test_host_helper_secrets_are_sent_over_stdin_not_arguments(self):
        completed = type(
            "Completed", (), {"returncode": 0, "stdout": '{"status":"configured"}', "stderr": ""}
        )()
        payload = {"operational_token": "secret-token", "server_response": {"password": "secret"}}
        with patch("server.edge_onboarding.host_helper.subprocess.run", return_value=completed) as run:
            self.assertEqual(
                run_host_helper("pair", payload, timeout=10),
                {"status": "configured"},
            )
        command = run.call_args.args[0]
        self.assertNotIn("secret-token", " ".join(command))
        self.assertIn("secret-token", run.call_args.kwargs["input"])


if __name__ == "__main__":
    unittest.main()
