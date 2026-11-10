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
from server.operations.services import (
    _deployment_state,
    _run,
    _tracking_status,
    get_optional_service_states,
    run_service_action,
)
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

    def test_start_rebuilds_only_currently_running_ai_services(self):
        installed = DeploymentContext(
            data_root=self.root,
            env_file=self.env,
            config_file=self.root / "config" / "config.yaml",
            public_url="http://127.0.0.1",
            rtsp_host="127.0.0.1",
            rtsp_port=8554,
            admin_username="admin",
            allow_insecure_http=True,
        )
        cases = [
            ([{"Service": "preprocessing", "State": "exited"}], ()),
            ([{"Service": "preprocessing", "State": "running"}], ("preprocessing",)),
            (
                [
                    {"Service": "preprocessing", "State": "running"},
                    {"Service": "analysis", "State": "restarting"},
                ],
                ("preprocessing", "analysis"),
            ),
        ]
        for rows, expected in cases:
            with self.subTest(expected=expected), patch(
                "server.operations.services._deployment_state", return_value="matching"
            ), patch(
                "server.operations.services._compose_ps_rows", return_value=rows
            ), patch(
                "server.operations.services.ComposeAdapter.deployment_prerequisites",
                return_value=[],
            ), patch(
                "server.operations.services._run", return_value=""
            ) as run_mock, patch(
                "server.operations.services._status", return_value="ok"
            ):
                result = run_service_action(
                    self.server, installed, "start", lambda _message: None
                )
                self.assertEqual(result, "ok")
                command = run_mock.call_args.args[0]
                suffix = list(expected) or ["up", "-d", "--build", "--wait"]
                self.assertEqual(command[-len(suffix):], suffix)

    def test_tracking_status_reports_stale_and_first_observation(self):
        adapter = ComposeAdapter(self.server, self.env)
        worker = {
            "model_ready": True,
            "capture_connected": True,
            "live_objects_last_success_at": "2026-11-10T04:58:00Z",
            "live_objects_stale": True,
        }
        payload = {"status": "degraded", "workers": {"local-camera": worker}}
        with patch("server.operations.services._run", return_value=json.dumps(payload)):
            self.assertIn("좌표 지연", _tracking_status(adapter, time.monotonic() + 10))
        worker["live_objects_stale"] = False
        worker["live_objects_last_success_at"] = None
        payload["status"] = "ready"
        with patch("server.operations.services._run", return_value=json.dumps(payload)):
            self.assertIn("첫 좌표 준비 중", _tracking_status(adapter, time.monotonic() + 10))
        worker["live_objects_last_success_at"] = "2026-11-10T04:58:00Z"
        with patch("server.operations.services._run", return_value=json.dumps(payload)):
            self.assertEqual("실시간 좌표 전달 중", _tracking_status(adapter, time.monotonic() + 10))


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

    def test_get_optional_service_states_reflects_running_and_stopped_containers(self):
        installed = DeploymentContext(
            data_root=self.root,
            env_file=self.env,
            config_file=self.root / "config" / "config.yaml",
            public_url="http://127.0.0.1",
            rtsp_host="127.0.0.1",
            rtsp_port=8554,
            admin_username="admin",
            allow_insecure_http=True,
        )
        rows = [
            {"Service": "preprocessing", "State": "running"},
            {"Service": "analysis", "State": "exited"},
        ]
        with patch(
            "server.operations.services._deployment_state", return_value="matching"
        ), patch("server.operations.services._compose_ps_rows", return_value=rows):
            states = get_optional_service_states(self.server, installed)
        self.assertEqual(states, {"preprocessing": True, "analysis": False})

        with patch(
            "server.operations.services._deployment_state", return_value="empty"
        ):
            empty_states = get_optional_service_states(self.server, installed)
        self.assertEqual(empty_states, {"preprocessing": False, "analysis": False})

    def test_clear_logs_wipes_media_snapshots_and_db_history_while_preserving_users_and_cameras(
        self,
    ):
        import contextlib
        import sqlite3

        config_file = self.root / "config" / "config.yaml"
        config_file.write_text("schema_version: 1\n", encoding="utf-8")
        db_dir = self.root / "database"
        recordings_dir = self.root / "recordings"
        recovered_dir = self.root / "recovered"
        snapshots_dir = self.root / "snapshots"
        logs_dir = self.root / "logs"
        for directory in (
            db_dir,
            recordings_dir / "cam-1",
            recovered_dir / "cam-1",
            snapshots_dir / "cam-1",
            logs_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)
        (recordings_dir / "cam-1" / "segment1.mp4").write_bytes(b"video-data")
        (recovered_dir / "cam-1" / "recovered1.mp4").write_bytes(b"recovered-data")
        (snapshots_dir / "cam-1" / "snap1.jpg").write_bytes(b"jpg-data")
        (snapshots_dir / ".event-outbox.sqlite3").write_bytes(b"outbox-data")
        (logs_dir / "server.log").write_text("log-entry\n", encoding="utf-8")

        self.env.write_text(
            f"CONFIG_FILE={config_file}\n"
            f"DATABASE_DIR={db_dir}\n"
            f"RECORDINGS_DIR={recordings_dir}\n"
            f"RECOVERED_DIR={recovered_dir}\n"
            f"SNAPSHOTS_DIR={snapshots_dir}\n"
            f"LOGS_DIR={logs_dir}\n",
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

        db_path = db_dir / "ai_cctv.db"
        migrations_dir = (
            Path(__file__).resolve().parents[1]
            / "services"
            / "data"
            / "app"
            / "database"
            / "migrations"
        )
        with contextlib.closing(sqlite3.connect(db_path)) as conn:
            for migration in sorted(migrations_dir.glob("*.sql")):
                conn.executescript(migration.read_text(encoding="utf-8"))
            conn.execute("PRAGMA foreign_keys = ON;")
            conn.execute(
                "INSERT INTO users (username, password_hash, role, is_active, created_at, updated_at) "
                "VALUES ('admin', 'hash', 'admin', 1, '2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z')"
            )
            conn.execute(
                "INSERT INTO cameras (camera_id, name, stream_path, enabled, recording_enabled, "
                "recording_last_error_code, created_at, updated_at) "
                "VALUES ('cam-1', 'Cam 1', 'cam-1', 1, 1, 'ERR_OLD', '2026-11-01T00:00:00Z', '2026-11-01T00:00:00Z')"
            )
            conn.execute(
                "INSERT INTO camera_runtime_status (camera_id, event_cursor, last_error_code, updated_at) "
                "VALUES ('cam-1', '55', 'ERR_OLD', '2026-11-01T00:00:00Z')"
            )
            conn.execute(
                "INSERT INTO recording_segments (id, camera_id, start_time, end_time, relative_path, "
                "format, codec, duration_ms, file_size, source, status, created_at, updated_at) "
                "VALUES (10, 'cam-1', '2026-11-01T00:00:00Z', '2026-11-01T00:01:00Z', 'cam-1/segment1.mp4', "
                "'mp4', 'h264', 60000, 100, 'central', 'ready', '2026-11-01T00:01:00Z', '2026-11-01T00:01:00Z')"
            )
            conn.execute(
                "INSERT INTO events (id, camera_id, event_type, person_id, global_person_id, confidence, "
                "occurred_at, metadata_json, created_at) "
                "VALUES (25, 'cam-1', 'person_appeared', '7', 1, 0.9, '2026-11-01T00:00:10Z', '{}', '2026-11-01T00:00:10Z')"
            )
            conn.execute(
                "INSERT INTO event_recording_segments (event_id, recording_segment_id, created_at) "
                "VALUES (25, 10, '2026-11-01T00:00:10Z')"
            )
            conn.execute(
                "INSERT INTO global_persons (id, created_at) VALUES (1, '2026-11-01T00:00:10Z')"
            )
            conn.execute(
                "INSERT INTO live_objects (camera_id, payload_json, observed_at, received_at) "
                "VALUES ('cam-1', '[]', '2026-11-01T00:00:10Z', '2026-11-01T00:00:10Z')"
            )
            conn.commit()

        executed_commands = []

        def fake_run(command, **_kwargs):
            executed_commands.append(command)
            return ""

        rows = [
            {"Service": "data", "State": "running"},
            {"Service": "preprocessing", "State": "running"},
            {"Service": "analysis", "State": "exited"},
        ]
        with patch(
            "server.operations.services._deployment_state", return_value="matching"
        ), patch(
            "server.operations.services._compose_ps_rows", return_value=rows
        ), patch(
            "server.operations.services._run", side_effect=fake_run
        ), patch(
            "server.operations.services.stop_publisher", return_value=False
        ):
            result = run_service_action(
                self.server, installed, "clear-logs", lambda _msg: None
            )

        self.assertIn("삭제된 이벤트 이력: 1건", result)
        self.assertIn("삭제된 녹화 세그먼트 이력: 1건", result)
        self.assertIn("삭제된 미디어·스냅샷·로그 파일: 5개", result)
        for directory in (recordings_dir, recovered_dir, snapshots_dir, logs_dir):
            self.assertTrue(directory.is_dir())
            self.assertEqual(list(directory.iterdir()), [])

        self.assertGreaterEqual(len(executed_commands), 3)
        self.assertEqual(executed_commands[0][-3:], ["--profile", "ai", "stop"])
        self.assertEqual(executed_commands[1][-3:], ["up", "-d", "--wait"])
        self.assertEqual(
            executed_commands[2][-5:],
            ["up", "-d", "--wait", "--no-deps", "preprocessing"],
        )

        with contextlib.closing(sqlite3.connect(db_path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM cameras").fetchone()[0], 1)
            self.assertIsNone(
                conn.execute(
                    "SELECT recording_last_error_code FROM cameras WHERE camera_id = 'cam-1'"
                ).fetchone()[0]
            )
            self.assertEqual(
                conn.execute(
                    "SELECT event_cursor, last_error_code FROM camera_runtime_status WHERE camera_id = 'cam-1'"
                ).fetchall(),
                [(None, None)],
            )
            for table in (
                "events",
                "recording_segments",
                "event_recording_segments",
                "global_persons",
                "live_objects",
            ):
                self.assertEqual(
                    conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0],
                    0,
                    table,
                )
            conn.execute(
                "INSERT INTO events (camera_id, event_type, occurred_at, metadata_json, created_at) "
                "VALUES ('cam-1', 'person_appeared', '2026-11-02T00:00:00Z', '{}', '2026-11-02T00:00:00Z')"
            )
            new_id = conn.execute("SELECT id FROM events").fetchone()[0]
            self.assertEqual(new_id, 1)


class ServerManagerToggleButtonTests(unittest.TestCase):
    def setUp(self):
        import os

        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt5.QtWidgets import QApplication

        self.app = QApplication.instance() or QApplication([])
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.server = self.root / "server"
        self.server.mkdir()
        self.env = self.root / "config" / "compose.env"
        self.env.parent.mkdir()
        self.env.write_text(
            "PUBLIC_SCHEME=http\n"
            "PUBLIC_BASE_URL=http://127.0.0.1\n"
            "PUBLIC_BIND_ADDRESS=127.0.0.1\n"
            "PUBLIC_HTTP_PORT=80\n"
            "PUBLIC_HTTPS_PORT=443\n"
            "RTSP_BIND_ADDRESS=127.0.0.1\n"
            "RTSP_PORT=8554\n"
            "ALLOW_INSECURE_HTTP=true\n"
            "COOKIE_SECURE=false\n",
            encoding="utf-8",
        )
        self.config_file = self.root / "config" / "config.yaml"
        self.config_file.write_text("schema_version: 1\n", encoding="utf-8")
        self.deployment = DeploymentContext(
            data_root=self.root,
            env_file=self.env,
            config_file=self.config_file,
            public_url="http://127.0.0.1",
            rtsp_host="127.0.0.1",
            rtsp_port=8554,
            admin_username="admin",
            allow_insecure_http=True,
        )

    def tearDown(self):
        self.temporary.cleanup()

    def _wait_task(self, window):
        if window._task is not None:
            window._task.wait(2000)
        self.app.processEvents()

    def test_buttons_show_turn_on_when_off_and_turn_off_when_on(self):
        from server.desktop.server_manager.application import ServerManagerWindow
        from PyQt5.QtWidgets import QMessageBox

        current_states = {"preprocessing": False, "analysis": True}
        actions = []

        def fake_get_states(_server_dir, _deployment):
            return dict(current_states)

        def fake_run_action(_server_dir, _deployment, action, _progress):
            actions.append(action)
            if action == "preprocessing-on":
                current_states["preprocessing"] = True
            elif action == "preprocessing-off":
                current_states["preprocessing"] = False
                current_states["analysis"] = False
            elif action == "analysis-on":
                current_states["preprocessing"] = True
                current_states["analysis"] = True
            elif action == "analysis-off":
                current_states["analysis"] = False
            return f"ran {action}"

        with patch(
            "server.desktop.server_manager.application.inspect_installation",
            return_value=self.deployment,
        ), patch(
            "server.desktop.server_manager.application.get_optional_service_states",
            side_effect=fake_get_states,
        ), patch(
            "server.desktop.server_manager.application.run_service_action",
            side_effect=fake_run_action,
        ):
            window = ServerManagerWindow(
                server_dir=self.server, data_root=self.root
            )
            self._wait_task(window)

            self.assertEqual(window.preprocessing_button.text(), "감지·식별 켜기")
            self.assertEqual(window.analysis_button.text(), "이벤트 분석 끄기")

            window.preprocessing_button.click()
            self._wait_task(window)
            self.assertEqual(actions[-1], "preprocessing-on")
            self.assertEqual(window.preprocessing_button.text(), "감지·식별 끄기")
            self.assertEqual(window.analysis_button.text(), "이벤트 분석 끄기")

            window.preprocessing_button.click()
            self._wait_task(window)
            self.assertEqual(actions[-1], "preprocessing-off")
            self.assertEqual(window.preprocessing_button.text(), "감지·식별 켜기")
            self.assertEqual(window.analysis_button.text(), "이벤트 분석 켜기")

            window.analysis_button.click()
            self._wait_task(window)
            self.assertEqual(actions[-1], "analysis-on")
            self.assertEqual(window.preprocessing_button.text(), "감지·식별 끄기")
            self.assertEqual(window.analysis_button.text(), "이벤트 분석 끄기")

            self.assertEqual(window.clear_logs_button.text(), "영상·이벤트 로그 비우기")
            with patch(
                "server.desktop.server_manager.application.QMessageBox.question",
                return_value=QMessageBox.No,
            ):
                window.clear_logs_button.click()
                self._wait_task(window)
            self.assertNotEqual(actions[-1], "clear-logs")

            with patch(
                "server.desktop.server_manager.application.QMessageBox.question",
                return_value=QMessageBox.Yes,
            ):
                window.clear_logs_button.click()
                self._wait_task(window)
            self.assertEqual(actions[-1], "clear-logs")
            window.close()
            window.deleteLater()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
