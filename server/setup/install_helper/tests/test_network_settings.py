import tempfile
import unittest
from pathlib import Path

import yaml

from server.setup.install_helper.network_settings import (
    NetworkSettings,
    load_network_settings,
    save_network_settings,
    validate_network_settings,
)


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


if __name__ == "__main__":
    unittest.main()
