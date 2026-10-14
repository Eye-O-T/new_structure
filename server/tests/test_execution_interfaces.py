from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.desktop.launcher import desktop_command
from server.edge_onboarding.host_helper import run_host_helper
from server.operations.local_camera import (
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
