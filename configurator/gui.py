from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Callable

from ai_cctv_core.config import CameraBootstrap

from .config_core import InstallRequest, initialize
from .compose_adapter import ComposeAdapter, default_server_dir
from .model_manager import install_from_manifest
from .server_api import (
    ServerApiClient,
    ServerApiError,
    prepare_private_output,
    redact_for_display,
    write_publish_credentials,
)


def rotate_publish_credentials_to_file(
    client: ServerApiClient, camera_id: str, output_path: str | Path
) -> dict[str, Any]:
    """Rotate a camera credential only after its private handoff is writable."""

    normalized_camera_id = camera_id.strip()
    if not normalized_camera_id:
        raise ValueError("camera ID is required")
    if isinstance(output_path, str) and not output_path.strip():
        raise ValueError("publish credentials output path is required")
    handoff_path = prepare_private_output(Path(output_path))
    result = client.rotate_publish_credentials(normalized_camera_id)
    write_publish_credentials(result, normalized_camera_id, handoff_path)
    result = dict(result)
    result["handoff_file"] = str(handoff_path)
    return result


def run() -> int:
    try:
        from PyQt5.QtWidgets import (
            QApplication,
            QComboBox,
            QFileDialog,
            QFormLayout,
            QLineEdit,
            QMessageBox,
            QPushButton,
            QScrollArea,
            QSpinBox,
            QTextEdit,
            QVBoxLayout,
            QWidget,
        )
    except ImportError as exc:
        raise RuntimeError("Install the configurator extra to use the GUI") from exc

    class Window(QWidget):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("AI CCTV Configurator")
            self.resize(720, 800)
            outer = QVBoxLayout(self)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            content = QWidget()
            form = QFormLayout(content)
            scroll.setWidget(content)
            outer.addWidget(scroll)
            default_root = Path(os.getenv("PROGRAMDATA", str(Path.home()))) / "AI_CCTV"
            self.storage = QLineEdit(str(default_root))
            choose = QPushButton("Choose storage")
            choose.clicked.connect(self.choose_storage)
            self.username = QLineEdit("admin")
            self.password = QLineEdit()
            self.password.setEchoMode(QLineEdit.Password)
            self.model = QLineEdit()
            choose_model = QPushButton("Choose model")
            choose_model.clicked.connect(self.choose_model)
            self.manifest = QLineEdit()
            choose_manifest = QPushButton("Choose model manifest")
            choose_manifest.clicked.connect(self.choose_manifest)
            self.cameras = QLineEdit("cam-001:Entrance")
            self.http = QSpinBox()
            self.http.setRange(1, 65535)
            self.http.setValue(80)
            self.https = QSpinBox()
            self.https.setRange(1, 65535)
            self.https.setValue(443)
            self.public_bind = QLineEdit("127.0.0.1")
            self.public_base_url = QLineEdit("https://127.0.0.1")
            self.public_base_url.setPlaceholderText("https://cctv.example.com")
            self.rtsp_bind = QLineEdit("127.0.0.1")
            self.rtsp_bind.setPlaceholderText(
                "Use the server's trusted-LAN IP for remote Edge publishers"
            )
            self.rtsp_port = QSpinBox()
            self.rtsp_port.setRange(1, 65535)
            self.rtsp_port.setValue(8554)
            submit = QPushButton("Validate and create configuration")
            submit.clicked.connect(self.submit)
            start = QPushButton("Start services")
            start.clicked.connect(lambda: self.service_action("start"))
            stop = QPushButton("Stop services")
            stop.clicked.connect(lambda: self.service_action("stop"))
            restart = QPushButton("Restart services")
            restart.clicked.connect(lambda: self.service_action("restart"))
            status = QPushButton("Show service status")
            status.clicked.connect(lambda: self.service_action("status"))

            self.server_url = QLineEdit("https://127.0.0.1")
            self.edge_camera_id = QLineEdit("cam-001")
            self.edge_name = QLineEdit("Entrance")
            self.edge_device_id = QLineEdit("edge-001")
            self.edge_management_url = QLineEdit("http://192.0.2.41:8003")
            self.edge_recovery_url = QLineEdit("http://192.0.2.41:8002")
            self.edge_auth_token = QLineEdit()
            self.edge_auth_token.setEchoMode(QLineEdit.Password)
            self.edge_auth_token.setPlaceholderText("minimum 32 characters")
            self.publish_credentials_output = QLineEdit(
                str(default_root / "secrets" / "cam-001-publish-credentials.json")
            )
            choose_publish_output = QPushButton("Choose credential handoff file")
            choose_publish_output.clicked.connect(
                self.choose_publish_credentials_output
            )
            self.video_profile = QComboBox()
            self.video_profile.addItems(["hd", "fhd"])
            register_edge = QPushButton("Register Edge and camera")
            register_edge.clicked.connect(self.register_edge)
            rotate_credentials = QPushButton("Rotate publish credential")
            rotate_credentials.clicked.connect(self.rotate_publish_credentials)
            update_edge = QPushButton("Update Edge settings")
            update_edge.clicked.connect(self.update_edge)
            edge_status = QPushButton("Query Edge status")
            edge_status.clicked.connect(self.query_edge_status)
            query_profile = QPushButton("Query video profile")
            query_profile.clicked.connect(self.query_video_profile)
            apply_profile = QPushButton("Apply selected video profile")
            apply_profile.clicked.connect(self.apply_video_profile)
            self.api_result = QTextEdit()
            self.api_result.setReadOnly(True)
            self.api_result.setPlaceholderText(
                "Edge status, applied profile, or a safe error code is shown here."
            )
            form.addRow("Storage root", self.storage)
            form.addRow("", choose)
            form.addRow("Administrator", self.username)
            form.addRow("Password", self.password)
            form.addRow("Model path", self.model)
            form.addRow("", choose_model)
            form.addRow("Or model manifest", self.manifest)
            form.addRow("", choose_manifest)
            form.addRow("Cameras (id:name, comma separated)", self.cameras)
            form.addRow("HTTP port", self.http)
            form.addRow("HTTPS port", self.https)
            form.addRow("Web bind address", self.public_bind)
            form.addRow("Public HTTPS origin", self.public_base_url)
            form.addRow("RTSP bind (trusted LAN)", self.rtsp_bind)
            form.addRow("RTSP port", self.rtsp_port)
            form.addRow("", submit)
            form.addRow("", start)
            form.addRow("", stop)
            form.addRow("", restart)
            form.addRow("", status)
            form.addRow("Management server URL", self.server_url)
            form.addRow("Managed camera ID", self.edge_camera_id)
            form.addRow("Managed camera name", self.edge_name)
            form.addRow("Edge device ID", self.edge_device_id)
            form.addRow("Edge management URL", self.edge_management_url)
            form.addRow("Edge recovery URL", self.edge_recovery_url)
            form.addRow("Edge bearer token", self.edge_auth_token)
            form.addRow("Publish credential handoff", self.publish_credentials_output)
            form.addRow("", choose_publish_output)
            form.addRow("Video profile", self.video_profile)
            form.addRow("", register_edge)
            form.addRow("", rotate_credentials)
            form.addRow("", update_edge)
            form.addRow("", edge_status)
            form.addRow("", query_profile)
            form.addRow("", apply_profile)
            form.addRow("Management result", self.api_result)

        def service_action(self, action: str):
            adapter = ComposeAdapter(default_server_dir())
            arguments = {
                "start": ("up", "-d", "--build", "--wait"),
                "stop": ("down",),
                "restart": ("restart",),
                "status": ("ps",),
            }[action]
            try:
                result = adapter.run(*arguments, capture=True)
            except OSError as exc:
                QMessageBox.critical(self, "Service action failed", str(exc))
                return
            output = (result.stdout or result.stderr or "No output").strip()
            if result.returncode == 0:
                QMessageBox.information(self, "Service action complete", output)
            else:
                QMessageBox.critical(self, "Service action failed", output)

        def choose_storage(self):
            selected = QFileDialog.getExistingDirectory(self, "Storage root")
            if selected:
                self.storage.setText(selected)

        def choose_model(self):
            selected, _filter = QFileDialog.getOpenFileName(
                self,
                "Select inference model",
                "",
                "YOLO models (*.pt *.onnx *.engine)",
            )
            if selected:
                self.model.setText(selected)

        def choose_manifest(self):
            selected, _filter = QFileDialog.getOpenFileName(
                self, "Select model manifest", "", "JSON manifest (*.json)"
            )
            if selected:
                self.manifest.setText(selected)

        def choose_publish_credentials_output(self):
            selected, _filter = QFileDialog.getSaveFileName(
                self,
                "Save one-time RTSP publish credential",
                self.publish_credentials_output.text(),
                "JSON files (*.json)",
            )
            if selected:
                self.publish_credentials_output.setText(selected)

        def _management_call(
            self, operation: Callable[[ServerApiClient], dict[str, Any]]
        ) -> dict[str, Any] | None:
            try:
                client = ServerApiClient(self.server_url.text())
                client.login(self.username.text(), self.password.text())
                result = operation(client)
            except ServerApiError as exc:
                status = (
                    f" HTTP {exc.status_code}" if exc.status_code is not None else ""
                )
                message = f"[ERROR] {exc.code}{status}: {exc.message}"
                self.api_result.setPlainText(message)
                QMessageBox.critical(self, "Management request failed", message)
                return None
            except (OSError, ValueError) as exc:
                message = f"[ERROR] CONFIGURATOR_INPUT: {exc}"
                self.api_result.setPlainText(message)
                QMessageBox.critical(self, "Management request failed", message)
                return None
            finally:
                self.edge_auth_token.clear()
            safe = redact_for_display(result)
            self.api_result.setPlainText(json.dumps(safe, ensure_ascii=False, indent=2))
            return result

        def register_edge(self):
            token = self.edge_auth_token.text()
            if not token:
                self.api_result.setPlainText(
                    "[ERROR] CONFIGURATOR_INPUT: Edge bearer token is required"
                )
                return
            try:
                handoff_path = prepare_private_output(
                    Path(self.publish_credentials_output.text())
                )
            except (OSError, ValueError) as exc:
                self.api_result.setPlainText(f"[ERROR] CREDENTIAL_HANDOFF: {exc}")
                return

            def register_and_save(client: ServerApiClient) -> dict[str, Any]:
                result = client.register_edge(
                    camera_id=self.edge_camera_id.text(),
                    name=self.edge_name.text(),
                    edge_device_id=self.edge_device_id.text(),
                    edge_management_url=self.edge_management_url.text(),
                    edge_recovery_url=self.edge_recovery_url.text(),
                    edge_auth_token=token,
                )
                write_publish_credentials(
                    result, self.edge_camera_id.text(), handoff_path
                )
                result = dict(result)
                result["handoff_file"] = str(handoff_path)
                return result

            result = self._management_call(register_and_save)
            if result is not None:
                QMessageBox.information(
                    self,
                    "Edge registration complete",
                    "The one-time RTSP publish credential was saved to the "
                    f"protected file:\n{handoff_path}\n"
                    "Transfer that file to the matching Edge setup, then remove "
                    "unneeded copies.",
                )

        def rotate_publish_credentials(self):
            camera_id = self.edge_camera_id.text().strip()
            output_path = self.publish_credentials_output.text().strip()
            if not camera_id or not output_path:
                message = (
                    "[ERROR] CREDENTIAL_HANDOFF: Select a camera and an explicit "
                    "publish credential handoff file"
                )
                self.api_result.setPlainText(message)
                QMessageBox.critical(self, "Credential rotation failed", message)
                return

            result = self._management_call(
                lambda client: rotate_publish_credentials_to_file(
                    client, camera_id, output_path
                )
            )
            if result is not None:
                QMessageBox.information(
                    self,
                    "Credential rotation complete",
                    "The replacement RTSP publish credential was saved to the "
                    "protected file:\n"
                    f"{result['handoff_file']}\n"
                    "Transfer that file to the selected Edge setup, then remove "
                    "unneeded copies.",
                )

        def update_edge(self):
            token = self.edge_auth_token.text() or None
            self._management_call(
                lambda client: client.update_edge(
                    self.edge_camera_id.text(),
                    edge_device_id=self.edge_device_id.text() or None,
                    edge_management_url=self.edge_management_url.text() or None,
                    edge_recovery_url=self.edge_recovery_url.text() or None,
                    edge_auth_token=token,
                )
            )

        def query_edge_status(self):
            self._management_call(
                lambda client: client.camera_status(self.edge_camera_id.text())
            )

        def query_video_profile(self):
            result = self._management_call(
                lambda client: client.video_profile(self.edge_camera_id.text())
            )
            if result is None:
                return
            supported = result.get("supported_profiles")
            if isinstance(supported, list):
                values = [item for item in supported if item in {"hd", "fhd"}]
                if values:
                    self.video_profile.clear()
                    self.video_profile.addItems(values)
            current = result.get("current_profile")
            index = self.video_profile.findText(str(current))
            if index >= 0:
                self.video_profile.setCurrentIndex(index)

        def apply_video_profile(self):
            self._management_call(
                lambda client: client.set_video_profile(
                    self.edge_camera_id.text(), self.video_profile.currentText()
                )
            )

        def submit(self):
            try:
                if bool(self.model.text().strip()) == bool(
                    self.manifest.text().strip()
                ):
                    raise ValueError("select exactly one model file or model manifest")
                model_path = Path(self.model.text())
                if self.manifest.text().strip():
                    model_path = install_from_manifest(
                        Path(self.manifest.text()),
                        Path(self.storage.text()) / "model-downloads",
                    )
                cameras = []
                for item in filter(
                    None, map(str.strip, self.cameras.text().split(","))
                ):
                    camera_id, _, name = item.partition(":")
                    cameras.append(
                        CameraBootstrap(camera_id=camera_id, name=name or camera_id)
                    )
                result = initialize(
                    InstallRequest(
                        data_root=Path(self.storage.text()),
                        server_dir=default_server_dir(),
                        admin_username=self.username.text(),
                        admin_password=self.password.text(),
                        model_path=model_path,
                        cameras=cameras,
                        public_http_port=self.http.value(),
                        public_https_port=self.https.value(),
                        public_bind_address=self.public_bind.text(),
                        public_base_url=self.public_base_url.text(),
                        rtsp_bind_address=self.rtsp_bind.text(),
                        rtsp_port=self.rtsp_port.value(),
                    )
                )
            except Exception as exc:
                QMessageBox.critical(
                    self,
                    "Configuration failed",
                    f"Cause: {exc}\nImpact: services were not changed.\nAction: correct the highlighted values and retry.",
                )
                return
            QMessageBox.information(
                self,
                "Configuration complete",
                f"Created {result.config_path}\n"
                f"Data secrets: {result.secrets_path}\n"
                f"External secrets: {result.external_secrets_path}\n"
                f"Inference secrets: {result.inference_secrets_path}\n"
                f"Media secrets: {result.media_secrets_path}\n"
                f"Camera credentials: {result.camera_credentials_path}\n"
                "Transfer each credential to its Edge setup, then use the service controls to start AI_CCTV.",
            )

    app = QApplication(sys.argv)
    window = Window()
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(run())
