from __future__ import annotations

import os
import sys
from pathlib import Path

from ai_cctv_core.config import CameraBootstrap

from .config_core import InstallRequest, initialize
from .compose_adapter import ComposeAdapter, default_server_dir
from .model_manager import install_from_manifest


def run() -> int:
    try:
        from PyQt5.QtWidgets import (
            QApplication,
            QFileDialog,
            QFormLayout,
            QLineEdit,
            QMessageBox,
            QPushButton,
            QSpinBox,
            QWidget,
        )
    except ImportError as exc:
        raise RuntimeError("Install the configurator extra to use the GUI") from exc

    class Window(QWidget):
        def __init__(self):
            super().__init__()
            self.setWindowTitle("AI CCTV Configurator")
            form = QFormLayout(self)
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
            self.rtsp_bind = QLineEdit("0.0.0.0")
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
            form.addRow("RTSP bind (trusted LAN)", self.rtsp_bind)
            form.addRow("RTSP port", self.rtsp_port)
            form.addRow("", submit)
            form.addRow("", start)
            form.addRow("", stop)
            form.addRow("", restart)
            form.addRow("", status)

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
                f"Camera credentials: {result.camera_credentials_path}\n"
                "Transfer each credential to its Edge setup, then use the service controls to start AI_CCTV.",
            )

    app = QApplication(sys.argv)
    window = Window()
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(run())
