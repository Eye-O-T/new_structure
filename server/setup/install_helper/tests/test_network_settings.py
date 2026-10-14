import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

import yaml

from server.operations.network import (
    NetworkSettings,
    apply_network_settings,
    load_network_settings,
    save_network_settings,
    validate_network_settings,
)
from server.deployment.context import DeploymentContext


class NetworkSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.server = self.root / "server"
        (self.server / "services" / "nginx").mkdir(parents=True)
        self.env = self.root / "compose.env"
        self.config = self.root / "config.yaml"
        self.env.write_text(
            "DATA_EXTERNAL_TOKEN=keep-token\nJWT_SECRET=keep-jwt\n"
            "PUBLIC_SCHEME=http\nPUBLIC_BASE_URL=http://192.168.0.10\n"
            "PUBLIC_BIND_ADDRESS=192.168.0.10\nPUBLIC_HTTP_PORT=80\n"
            "PUBLIC_HTTPS_PORT=443\nRTSP_BIND_ADDRESS=192.168.0.10\nRTSP_PORT=8554\n"
            "ALLOW_INSECURE_HTTP=true\nCOOKIE_SECURE=false\nNGINX_CONFIG_FILE=old.conf\n",
            encoding="utf-8",
        )
        self.config.write_text(
            yaml.safe_dump({"schema_version": 1, "server": {"public_http_port": 80,
                "public_https_port": 443, "rtsp_bind_address": "192.168.0.10", "rtsp_port": 8554},
                "recording": {"root": "/recordings"}, "inference": {"device": "cpu"}}, sort_keys=False),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_save_changes_only_network_values(self):
        settings = NetworkSettings("http", "http://192.168.0.20:8080", "192.168.0.20",
                                   8080, 8443, "192.168.0.20", 8555, True, False, Path())
        save_network_settings(settings, self.env, self.config, self.server)
        values = self.env.read_text(encoding="utf-8")
        self.assertIn("DATA_EXTERNAL_TOKEN=keep-token", values)
        self.assertIn("JWT_SECRET=keep-jwt", values)
        self.assertIn("PUBLIC_BIND_ADDRESS=192.168.0.20", values)
        self.assertIn("RTSP_PUBLIC_HOST=192.168.0.20", values)
        self.assertIn("RTSP_PUBLIC_PORT=8555", values)
        self.assertIn("NGINX_CONFIG_FILE=", values)
        self.assertTrue((self.env.parent / "compose.env.bak").is_file())
        config = yaml.safe_load(self.config.read_text(encoding="utf-8"))
        self.assertEqual(config["server"]["rtsp_port"], 8555)

    def test_duplicate_ports_rejected(self):
        settings = NetworkSettings("http", "http://192.168.0.20", "192.168.0.20",
                                   80, 80, "192.168.0.20", 8554, True, False, Path())
        with self.assertRaises(ValueError):
            validate_network_settings(settings)

    def test_https_requires_tls_files(self):
        settings = NetworkSettings("https", "https://192.168.0.20", "192.168.0.20",
                                   80, 443, "192.168.0.20", 8554, False, True, Path())
        with self.assertRaises(ValueError):
            validate_network_settings(settings)

    def test_load_reads_existing_values(self):
        settings = load_network_settings(self.env, self.server)
        self.assertEqual(settings.public_scheme, "http")
        self.assertEqual(settings.rtsp_port, 8554)

    def test_apply_restores_files_when_compose_validation_fails(self):
        original_env = self.env.read_text(encoding="utf-8")
        original_config = self.config.read_text(encoding="utf-8")
        deployment = DeploymentContext(
            data_root=self.root,
            env_file=self.env,
            config_file=self.config,
            public_url="http://192.168.0.10",
            rtsp_host="192.168.0.10",
            rtsp_port=8554,
            admin_username="admin",
            allow_insecure_http=True,
        )
        settings = NetworkSettings(
            "http", "http://192.168.0.20:8080", "192.168.0.20", 8080,
            8443, "192.168.0.20", 8555, True, False, Path(),
        )
        failed_config = CompletedProcess([], 1, stdout="", stderr="invalid compose")
        with patch("server.operations.network.ComposeAdapter.run", return_value=failed_config), patch(
            "server.operations.services.run_service_action", return_value="restored"
        ) as restart:
            with self.assertRaisesRegex(RuntimeError, "invalid compose"):
                apply_network_settings(settings, deployment, self.server, lambda _message: None)
        self.assertEqual(self.env.read_text(encoding="utf-8"), original_env)
        self.assertEqual(self.config.read_text(encoding="utf-8"), original_config)
        restart.assert_not_called()

    def test_apply_restarts_previous_deployment_after_start_failure(self):
        original_env = self.env.read_text(encoding="utf-8")
        original_config = self.config.read_text(encoding="utf-8")
        deployment = DeploymentContext(
            data_root=self.root,
            env_file=self.env,
            config_file=self.config,
            public_url="http://192.168.0.10",
            rtsp_host="192.168.0.10",
            rtsp_port=8554,
            admin_username="admin",
            allow_insecure_http=True,
        )
        settings = NetworkSettings(
            "http", "http://192.168.0.20:8080", "192.168.0.20", 8080,
            8443, "192.168.0.20", 8555, True, False, Path(),
        )
        with patch(
            "server.operations.network.ComposeAdapter.run",
            return_value=CompletedProcess([], 0, stdout="", stderr=""),
        ), patch(
            "server.operations.services.run_service_action",
            side_effect=[RuntimeError("start failed"), "previous deployment restored"],
        ) as restart:
            with self.assertRaisesRegex(RuntimeError, "start failed"):
                apply_network_settings(settings, deployment, self.server, lambda _message: None)
        self.assertEqual(self.env.read_text(encoding="utf-8"), original_env)
        self.assertEqual(self.config.read_text(encoding="utf-8"), original_config)
        self.assertEqual(restart.call_count, 2)


if __name__ == "__main__":
    unittest.main()
