from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from server.deployment.context import DeploymentContext
from server.desktop.launcher import admin_arguments, desktop_command, server_manager_arguments


class DesktopLauncherTests(unittest.TestCase):
    def test_source_command_uses_module_entrypoint(self):
        self.assertEqual(
            desktop_command("server_manager", ("--data-root", "C:/data")),
            [
                sys.executable,
                "-m",
                "server.desktop.server_manager",
                "--data-root",
                "C:/data",
            ],
        )

    def test_frozen_command_uses_sibling_executable(self):
        with patch("server.desktop.launcher.sys.frozen", True, create=True), patch(
            "server.desktop.launcher.sys.executable", "C:/AI_CCTV/AI_CCTV_Server_Installer.exe"
        ):
            self.assertEqual(
                desktop_command("admin"),
                [str(Path("C:/AI_CCTV") / "AI_CCTV_Admin.exe")],
            )

    def test_unknown_tool_is_rejected(self):
        with self.assertRaises(ValueError):
            desktop_command("unknown")

    def test_server_manager_arguments_target_the_selected_deployment(self):
        self.assertEqual(
            server_manager_arguments(Path("C:/server"), Path("C:/data")),
            (
                "--server-dir",
                str(Path("C:/server")),
                "--data-root",
                str(Path("C:/data")),
            ),
        )

    def test_admin_arguments_include_connection_details_and_certificate(self):
        with self.subTest("certificate present"):
            with patch.object(Path, "is_file", return_value=True):
                arguments = admin_arguments(
                    DeploymentContext(
                        data_root=Path("C:/data"),
                        env_file=Path("C:/data/config/compose.env"),
                        config_file=Path("C:/server/config.yaml"),
                        public_url="https://cctv.example",
                        rtsp_host="10.0.0.2",
                        rtsp_port=8554,
                        admin_username="admin",
                    )
                )
        self.assertEqual(
            arguments,
            (
                "--server-url", "https://cctv.example", "--username", "admin",
                "--storage-path", str(Path("C:/data")), "--rtsp-host", "10.0.0.2",
                "--rtsp-port", "8554", "--ca-file",
                str(Path("C:/data/certs/tls.crt")),
            ),
        )
