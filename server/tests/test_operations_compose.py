import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from server.deployment.context import DeploymentContext
from server.deployment.signature import reset_incompatible_deployment
from server.operations.compose import (
    ComposeAdapter,
    START_ARGUMENTS,
    STOP_ARGUMENTS,
    default_server_dir,
)
from server.operations.services import _deployment_state, _run
from server.operations.doctor import _compose_rows


class DefaultServerDirTests(unittest.TestCase):
    def test_source_checkout_points_to_server_directory(self):
        server_dir = default_server_dir()

        self.assertEqual(server_dir, Path(__file__).resolve().parents[1])
        self.assertTrue((server_dir / "compose.yml").is_file())


class ComposeAdapterCommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.server = self.root / "server"
        self.server.mkdir()
        (self.server / "compose.yml").write_text("services: {}\n", encoding="utf-8")
        self.env = self.root / "config" / "compose.env"
        self.env.parent.mkdir()
        self.env.write_text("PREPROCESSING_GPU=false\n", encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def test_base_command_uses_selected_deployment(self):
        adapter = ComposeAdapter(self.server, self.env)

        command = adapter.command(*START_ARGUMENTS)

        self.assertEqual(
            command,
            [
                "docker",
                "compose",
                "--env-file",
                str(self.env.resolve()),
                "-f",
                str((self.server / "compose.yml").resolve()),
                "up",
                "-d",
                "--build",
                "--wait",
            ],
        )

    def test_push_and_gpu_overrides_are_selected_from_env(self):
        (self.server / "compose.push.yml").write_text("services: {}\n", encoding="utf-8")
        (self.server / "compose.gpu.yml").write_text("services: {}\n", encoding="utf-8")
        self.env.write_text("PREPROCESSING_GPU=true\n", encoding="utf-8")
        push = self.env.with_name("push.env")
        push.write_text("PUSH_ENABLED=true\n", encoding="utf-8")

        command = ComposeAdapter(self.server, self.env).command("config", "--quiet")

        self.assertIn(str(push.resolve()), command)
        self.assertIn(str((self.server / "compose.push.yml").resolve()), command)
        self.assertIn(str((self.server / "compose.gpu.yml").resolve()), command)
        self.assertEqual(command[-2:], ["config", "--quiet"])

    def test_stop_uses_profile_stop_without_down(self):
        adapter = ComposeAdapter(self.server, self.env)
        with patch.object(
            adapter, "run", return_value=SimpleNamespace(returncode=0)
        ) as run_mock:
            rc = adapter.stop()
        self.assertEqual(rc, 0)
        run_mock.assert_called_once_with(*STOP_ARGUMENTS)
        self.assertEqual(STOP_ARGUMENTS, ("--profile", "ai", "stop"))

    def test_compose_adapter_decodes_unicode_as_utf8(self):
        adapter = ComposeAdapter(self.server, self.env)
        with patch("server.operations.compose.subprocess.run") as run_mock:
            run_mock.return_value = subprocess.CompletedProcess([], 0, "ok", "")
            adapter.run("version", capture=True)
        self.assertEqual(run_mock.call_args.kwargs["encoding"], "utf-8")
        self.assertEqual(run_mock.call_args.kwargs["errors"], "replace")

    def test_diagnostic_rows_reject_missing_output(self):
        with self.assertRaisesRegex(ValueError, "output is unavailable"):
            _compose_rows(None)

    def test_diagnostic_rows_accept_unicode_json_output(self):
        self.assertEqual(
            _compose_rows('{"Service":"preprocessing","Status":"점검 중"}'),
            [{"Service": "preprocessing", "Status": "점검 중"}],
        )

    def test_start_failure_reports_missing_package_and_redacts_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(RuntimeError, "test-package"):
                _run(
                    [
                        sys.executable,
                        "-c",
                        "import sys; print('No matching distribution found for test-package', file=sys.stderr); sys.exit(1)",
                    ],
                    directory=Path(directory),
                    deadline=time.monotonic() + 10,
                    capture=False,
                )

            with self.assertRaises(RuntimeError) as raised:
                _run(
                    [
                        sys.executable,
                        "-c",
                        "import sys; print('rtsp://reader:secret@camera/stream'); print('DATA_INFERENCE_TOKEN=private-token'); sys.exit(1)",
                    ],
                    directory=Path(directory),
                    deadline=time.monotonic() + 10,
                    capture=False,
                )
        self.assertIn("<redacted>", str(raised.exception))
        self.assertNotIn("secret", str(raised.exception))
        self.assertNotIn("private-token", str(raised.exception))

    def test_reset_incompatible_deployment_stops_instead_of_down(self):
        with patch(
            "server.operations.compose.ComposeAdapter.run",
            return_value=SimpleNamespace(returncode=0),
        ) as run_mock:
            reset_incompatible_deployment(
                self.root, self.server, stop_services=True
            )
        run_mock.assert_called_once_with(*STOP_ARGUMENTS, capture=True)

    def test_deployment_state_matches_remaining_ai_containers_when_data_container_missing(
        self,
    ):
        config_file = self.root / "config" / "config.yaml"
        config_file.write_text("schema_version: 1\n", encoding="utf-8")
        db_dir = self.root / "database"
        snapshots_dir = self.root / "snapshots"
        models_dir = self.root / "models"
        for directory in (db_dir, snapshots_dir, models_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self.env.write_text(
            f"CONFIG_FILE={config_file}\n"
            f"DATABASE_DIR={db_dir}\n"
            f"SNAPSHOTS_DIR={snapshots_dir}\n"
            f"MODELS_DIR={models_dir}\n",
            encoding="utf-8",
        )
        installed = DeploymentContext(
            data_root=self.root,
            env_file=self.env,
            config_file=config_file,
            public_url="http://127.0.0.1",
            rtsp_host="127.0.0.1",
            rtsp_port=8554,
            admin_username="admin",
            allow_insecure_http=True,
        )
        adapter = ComposeAdapter(self.server, self.env)
        compose_config = {
            "name": "ai-cctv",
            "services": {
                "data": {
                    "volumes": [
                        {
                            "type": "bind",
                            "source": str(config_file),
                            "target": "/app/config/config.yaml",
                        },
                        {
                            "type": "bind",
                            "source": str(db_dir),
                            "target": "/data/database",
                        },
                    ]
                }
            },
        }
        ai_mounts = [
            {
                "Type": "bind",
                "Source": str(snapshots_dir),
                "Destination": "/snapshots",
            },
            {
                "Type": "bind",
                "Source": str(models_dir),
                "Destination": "/models",
            },
        ]

        def fake_run(command, **_kwargs):
            if "config" in command:
                return json.dumps(compose_config)
            if any(arg == "label=com.docker.compose.service=data" for arg in command):
                return ""
            if command[:2] == ["docker", "ps"]:
                return "a1b2c3d4e5f6\nb6e5d4c3b2a1\n"
            if command[:2] == ["docker", "inspect"]:
                return (
                    json.dumps(ai_mounts)
                    + "\n"
                    + json.dumps(ai_mounts)
                    + "\n"
                )
            raise AssertionError(f"Unexpected command: {command}")

        with patch("server.operations.services._run", side_effect=fake_run):
            state = _deployment_state(adapter, installed, deadline=999999999.0)
        self.assertEqual(state, "matching")


if __name__ == "__main__":
    unittest.main()
