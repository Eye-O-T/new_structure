from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from server.deployment.signature import (
    ContractSignatureMismatchError,
    compute_contract_signature,
    deployment_signature_path,
    project_signature_path,
    reset_incompatible_deployment,
    write_contract_signature,
)
from server.installer.configuration import InstallRequest, initialize
from server.installer.workflow import inspect_installation


class ContractSignatureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.data_root = self.root / "data_root"
        self.server_dir = self.root / "server"
        (self.server_dir / "services" / "nginx").mkdir(parents=True)
        (self.server_dir / "services" / "nginx" / "nginx.http.conf").write_text(
            "events {}\n", encoding="utf-8"
        )
        (self.server_dir / "compose.yml").write_text(
            "name: ai-cctv\nservices: {}\n", encoding="utf-8"
        )
        migrations_dir = (
            self.server_dir / "services" / "data" / "app" / "database" / "migrations"
        )
        migrations_dir.mkdir(parents=True)
        (migrations_dir / "001_initial.sql").write_text(
            "CREATE TABLE users (id INTEGER PRIMARY KEY);\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _make_install_request(self) -> InstallRequest:
        return InstallRequest(
            data_root=self.data_root,
            server_dir=self.server_dir,
            admin_username="admin",
            admin_password="password123",
            model_path=None,
            cameras=[],
            compose_env_path=self.data_root / "config" / "compose.env",
            public_scheme="http",
            allow_insecure_http=True,
            public_base_url="http://127.0.0.1",
            public_bind_address="127.0.0.1",
            rtsp_bind_address="127.0.0.1",
        )

    def test_initialize_creates_signature_files(self) -> None:
        initialize(self._make_install_request())
        deploy_sig = deployment_signature_path(self.data_root)
        proj_sig = project_signature_path(self.server_dir)
        self.assertTrue(deploy_sig.is_file())
        self.assertTrue(proj_sig.is_file())

        expected = compute_contract_signature(self.server_dir)
        stored = json.loads(deploy_sig.read_text(encoding="utf-8"))
        self.assertEqual(stored["combined_sha256"], expected.combined_sha256)
        self.assertEqual(stored["components"], expected.components)

    def test_missing_signature_is_created_on_first_load(self) -> None:
        initialize(self._make_install_request())
        deploy_sig = deployment_signature_path(self.data_root)
        proj_sig = project_signature_path(self.server_dir)
        deploy_sig.unlink()
        proj_sig.unlink()

        installed = inspect_installation(self.data_root, self.server_dir)
        self.assertIsNotNone(installed)
        self.assertTrue(deploy_sig.is_file())
        self.assertTrue(proj_sig.is_file())

    def test_contract_change_raises_mismatch_and_reset_preserves_models_and_certs(
        self,
    ) -> None:
        initialize(self._make_install_request())
        # Populate dummy DB, model, and TLS files
        db_file = self.data_root / "database" / "ai_cctv.db"
        db_file.write_bytes(b"sqlite-data")
        model_file = self.data_root / "models" / "yolo11n.pt"
        model_file.write_bytes(b"model-weights")
        cert_file = self.data_root / "certs" / "tls.crt"
        key_file = self.data_root / "certs" / "tls.key"
        cert_file.write_text("CERT", encoding="utf-8")
        key_file.write_text("KEY", encoding="utf-8")

        # Simulate a refactoring that modifies the DB schema migration SQL
        sql_path = (
            self.server_dir
            / "services"
            / "data"
            / "app"
            / "database"
            / "migrations"
            / "001_initial.sql"
        )
        sql_path.write_text(
            "CREATE TABLE users (id INTEGER PRIMARY KEY, refactored_col TEXT);\n",
            encoding="utf-8",
        )

        with self.assertRaises(ContractSignatureMismatchError) as ctx:
            inspect_installation(self.data_root, self.server_dir)

        self.assertIn("db_schema", ctx.exception.changed_components)
        self.assertIn("데이터베이스 마이그레이션 스키마", ctx.exception.user_message)

        summary = reset_incompatible_deployment(
            self.data_root, self.server_dir, stop_services=False
        )
        self.assertIn("models", summary["preserved"])
        self.assertIn("certs", summary["preserved"])
        self.assertTrue(model_file.is_file())
        self.assertTrue(cert_file.is_file())
        self.assertTrue(key_file.is_file())
        self.assertFalse(db_file.exists())
        self.assertFalse((self.data_root / "config" / "compose.env").exists())
        self.assertFalse((self.data_root / "secrets").exists())

        # After reset, inspect_installation sees a clean directory ready for fresh install
        self.assertIsNone(inspect_installation(self.data_root, self.server_dir))

    def test_cli_inspect_or_reset_clears_data_and_prompts_reconfiguration(self) -> None:
        from server.cli import _inspect_or_reset_installation

        initialize(self._make_install_request())
        model_file = self.data_root / "models" / "yolo11n.pt"
        model_file.write_bytes(b"model-weights")
        cert_file = self.data_root / "certs" / "tls.crt"
        cert_file.write_text("CERT", encoding="utf-8")

        # Tamper with stored signature to simulate contract change
        deploy_sig = deployment_signature_path(self.data_root)
        payload = json.loads(deploy_sig.read_text(encoding="utf-8"))
        payload["components"]["domain_contract"] = "0" * 64
        payload["combined_sha256"] = "f" * 64
        deploy_sig.write_text(json.dumps(payload), encoding="utf-8")

        def fake_prompt(data_root: Path, server_dir: Path, _notice: str) -> None:
            # Simulate user entering new configuration values after reset
            req = InstallRequest(
                data_root=data_root,
                server_dir=server_dir,
                admin_username="newadmin",
                admin_password="newpass123",
                model_path=None,
                cameras=[],
                compose_env_path=data_root / "config" / "compose.env",
                public_scheme="http",
                allow_insecure_http=True,
                public_base_url="http://127.0.0.1",
            )
            initialize(req)

        with patch("server.cli._prompt_reconfiguration", side_effect=fake_prompt):
            deployment = _inspect_or_reset_installation(self.data_root, self.server_dir)

        self.assertIsNotNone(deployment)
        self.assertEqual(deployment.admin_username, "newadmin")
        self.assertTrue(model_file.is_file())
        self.assertTrue(cert_file.is_file())

    def test_installer_window_resets_and_discovers_preserved_files_on_mismatch(
        self,
    ) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt5.QtWidgets import QApplication
        from server.installer.gui import InstallerWindow

        app = QApplication.instance() or QApplication([])
        initialize(self._make_install_request())
        model_file = self.data_root / "models" / "yolo11n.pt"
        model_file.write_bytes(b"model-weights")
        cert_file = self.data_root / "certs" / "tls.crt"
        key_file = self.data_root / "certs" / "tls.key"
        cert_file.write_text("CERT", encoding="utf-8")
        key_file.write_text("KEY", encoding="utf-8")

        # Tamper with stored signature
        deploy_sig = deployment_signature_path(self.data_root)
        payload = json.loads(deploy_sig.read_text(encoding="utf-8"))
        payload["components"]["deployment_contract"] = "1" * 64
        payload["combined_sha256"] = "e" * 64
        deploy_sig.write_text(json.dumps(payload), encoding="utf-8")

        notices: list[tuple[str, str]] = []
        with patch.object(
            InstallerWindow,
            "_show_notice",
            side_effect=lambda title, msg: notices.append((title, msg)),
        ):
            window = InstallerWindow(
                server_dir=self.server_dir,
                data_root=self.data_root,
                auto_check=False,
            )
        try:
            self.assertIsNone(window.installation)
            self.assertFalse(window._location_error)
            self.assertEqual(window.pages.currentIndex(), 0)
            self.assertEqual(
                window.title.text(), "AI CCTV 서버 재설정 (계약 변경 감지)"
            )
            self.assertEqual(Path(window.model.text()), model_file.resolve())
            self.assertEqual(Path(window.tls_certificate.text()), cert_file.resolve())
            self.assertEqual(Path(window.tls_private_key.text()), key_file.resolve())
            self.assertIn("배포/설정/시크릿 계약", window.check_results.toPlainText())
            self.assertEqual(len(notices), 1)
            self.assertIn("계약 변경 감지", notices[0][0])
            self.assertIn("배포/설정/시크릿 계약", notices[0][1])
        finally:
            window.close()
            window.deleteLater()

    def test_installer_window_with_external_reset_notice_preserves_notice_after_preflight(
        self,
    ) -> None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from PyQt5.QtWidgets import QApplication
        from server.installer.gui import InstallerWindow
        from server.operations.compose import Prerequisite

        app = QApplication.instance() or QApplication([])
        initialize(self._make_install_request())
        reset_incompatible_deployment(
            self.data_root, self.server_dir, stop_services=False
        )
        notice_text = "시스템 내부 계약 또는 코드 변경이 감지되었습니다.\n- 변경된 영역: 배포/설정/시크릿 계약"

        notices: list[tuple[str, str]] = []
        with patch.object(
            InstallerWindow,
            "_show_notice",
            side_effect=lambda title, msg: notices.append((title, msg)),
        ), patch(
            "server.installer.gui.preflight",
            return_value=[
                Prerequisite(False, "HTTPS 인증서와 개인키", "인증서를 선택하세요.")
            ],
        ):
            window = InstallerWindow(
                server_dir=self.server_dir,
                data_root=self.data_root,
                auto_check=False,
                reset_notice=notice_text,
            )
            try:
                self.assertEqual(
                    window.title.text(), "AI CCTV 서버 재설정 (계약 변경 감지)"
                )
                self.assertEqual(len(notices), 1)
                self.assertIn("계약 변경 감지", notices[0][0])

                # Simulate auto_check completion synchronously
                window.check_current_location()
                if window._task is not None:
                    window._task.wait(5000)
                app.processEvents()

                # Initial preflight failure must not overwrite the contract reset notice popup
                self.assertEqual(len(notices), 1)
                results_text = window.check_results.toPlainText()
                self.assertIn("시스템 내부 계약 또는 코드 변경이 감지되었습니다.", results_text)
                self.assertIn("[준비 필요] HTTPS 인증서와 개인키", results_text)
            finally:
                window.close()
                window.deleteLater()

    def test_cli_prompt_reconfiguration_retries_on_invalid_input(self) -> None:
        from server.cli import _prompt_reconfiguration

        self.data_root.mkdir(parents=True, exist_ok=True)
        inputs = iter(["admin", "http", "http://127.0.0.1", "admin2", "http", "http://127.0.0.1"])
        passwords = iter(["12", "validpass12"])

        with patch.dict(os.environ, {"AI_CCTV_NO_GUI_ON_RESET": "1"}), patch(
            "sys.stdin.isatty", return_value=True
        ), patch("builtins.input", side_effect=lambda _prompt="": next(inputs)), patch(
            "getpass.getpass", side_effect=lambda _prompt="": next(passwords)
        ):
            _prompt_reconfiguration(self.data_root, self.server_dir, "계약 변경 안내")

        installed = inspect_installation(self.data_root, self.server_dir)
        self.assertIsNotNone(installed)
        self.assertEqual(installed.admin_username, "admin2")


if __name__ == "__main__":
    unittest.main()
