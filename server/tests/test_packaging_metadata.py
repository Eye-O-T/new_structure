from __future__ import annotations

import tomllib
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class PackagingMetadataTests(unittest.TestCase):
    def test_project_metadata_is_valid_toml(self):
        for path in (
            ROOT / "server" / "setup" / "pyproject.toml",
            ROOT / "server" / "setup" / "install_helper" / "pyproject.toml",
        ):
            with self.subTest(path=path):
                metadata = tomllib.loads(path.read_text(encoding="utf-8"))
                self.assertIn("project", metadata)
                self.assertIn("package-dir", metadata["tool"]["setuptools"])

    def test_host_tool_scripts_use_canonical_entrypoints(self):
        path = ROOT / "server" / "setup" / "install_helper" / "pyproject.toml"
        scripts = tomllib.loads(path.read_text(encoding="utf-8"))["project"]["scripts"]
        self.assertEqual(scripts["ai-cctv-server-installer"], "server.installer.gui_entry:run")
        self.assertEqual(scripts["ai-cctv-server-manager"], "server.desktop.server_manager.application:main")
        self.assertEqual(scripts["ai-cctv-admin"], "server.desktop.admin.application:main")
