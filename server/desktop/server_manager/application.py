"""Windows host UI for lifecycle, network, AI, and diagnostic operations."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PyQt5.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from server.deployment.validation import deployment_path, read_deployment_env
from server.desktop.common.tasks import BackgroundTask
from server.desktop.launcher import admin_arguments, launch_desktop
from server.installer.workflow import inspect_installation
from server.operations.compose import default_data_root, default_server_dir
from server.operations.doctor import checks
from server.operations.network import (
    NetworkSettings,
    apply_network_settings,
    load_network_settings,
    validate_network_settings,
)
from server.operations.services import run_service_action


class ServerManagerWindow(QWidget):
    """Thin Qt frontend; all mutations are delegated to operations modules."""

    def __init__(self, *, server_dir: Path, data_root: Path) -> None:
        super().__init__()
        self.server_dir = server_dir.expanduser().resolve()
        self.data_root = data_root.expanduser().resolve()
        self.deployment = inspect_installation(self.data_root, self.server_dir)
        if self.deployment is None:
            raise ValueError(
                "설치된 서버 설정이 없습니다. 먼저 AI CCTV Server Installer를 실행하세요."
            )
        self._task: BackgroundTask | None = None
        self.setWindowTitle("AI CCTV 서버 관리자")
        self.resize(760, 720)

        layout = QVBoxLayout(self)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        layout.addWidget(self._service_group())
        layout.addWidget(self._desktop_group())
        layout.addWidget(self._network_group())

        self.output = QPlainTextEdit()
        self.output.setReadOnly(True)
        layout.addWidget(self.output, 1)
        self.activity = QLabel("준비됨")
        layout.addWidget(self.activity)

        self._load_network()
        self._refresh_summary()

    def _service_group(self) -> QGroupBox:
        group = QGroupBox("서버 운영")
        grid = QGridLayout(group)
        actions = (
            ("빌드 및 시작", "start"),
            ("재시작", "restart"),
            ("중지", "stop"),
            ("상태 확인", "status"),
            ("감지·식별 켜기", "preprocessing-on"),
            ("감지·식별 끄기", "preprocessing-off"),
            ("이벤트 분석 켜기", "analysis-on"),
            ("이벤트 분석 끄기", "analysis-off"),
        )
        self.action_buttons = []
        for index, (label, action) in enumerate(actions):
            button = QPushButton(label)
            button.clicked.connect(
                lambda _checked=False, selected=action: self._service_action(selected)
            )
            grid.addWidget(button, index // 2, index % 2)
            self.action_buttons.append(button)
        doctor = QPushButton("통합 진단")
        doctor.clicked.connect(self._doctor)
        grid.addWidget(doctor, 4, 0, 1, 2)
        self.action_buttons.append(doctor)
        return group

    def _desktop_group(self) -> QGroupBox:
        group = QGroupBox("Desktop tools")
        layout = QHBoxLayout(group)
        button = QPushButton("Open CCTV Admin")
        button.clicked.connect(self._open_admin)
        layout.addWidget(button)
        return group

    def _open_admin(self) -> None:
        try:
            process = launch_desktop("admin", admin_arguments(self.deployment))
        except OSError as exc:
            self.output.setPlainText(f"Unable to start CCTV Admin: {exc}")
            return
        self.activity.setText(f"CCTV Admin started (PID {process.pid})")

    def _network_group(self) -> QGroupBox:
        group = QGroupBox("네트워크 설정")
        form = QFormLayout(group)
        self.scheme = QComboBox()
        self.scheme.addItem("HTTPS", "https")
        self.scheme.addItem("HTTP (개발용)", "http")
        self.public_url = QLineEdit()
        self.public_bind = QLineEdit()
        self.rtsp_bind = QLineEdit()
        self.tls_certificate = QLineEdit()
        self.tls_private_key = QLineEdit()
        self.http_port = QSpinBox()
        self.https_port = QSpinBox()
        self.rtsp_port = QSpinBox()
        for widget in (self.http_port, self.https_port, self.rtsp_port):
            widget.setRange(1, 65535)
        form.addRow("통신 방식", self.scheme)
        form.addRow("공개 접속 주소", self.public_url)
        form.addRow("웹 Bind IP", self.public_bind)
        form.addRow("HTTP 포트", self.http_port)
        form.addRow("HTTPS 포트", self.https_port)
        form.addRow("RTSP Bind IP", self.rtsp_bind)
        form.addRow("RTSP 포트", self.rtsp_port)
        form.addRow("TLS 인증서", self._file_row(self.tls_certificate, "TLS 인증서 선택", "Certificates (*.crt *.pem);;All files (*)"))
        form.addRow("TLS 개인키", self._file_row(self.tls_private_key, "TLS 개인키 선택", "Keys (*.key *.pem);;All files (*)"))
        row = QHBoxLayout()
        validate = QPushButton("검증")
        validate.clicked.connect(self._validate_network)
        apply = QPushButton("적용 및 서버 재구성")
        apply.clicked.connect(self._apply_network)
        row.addWidget(validate)
        row.addWidget(apply)
        form.addRow(row)
        self.network_buttons = [validate, apply]
        return group

    def _file_row(self, widget: QLineEdit, title: str, file_filter: str) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(widget, 1)
        button = QPushButton("선택…")
        button.clicked.connect(
            lambda _checked=False: self._choose_file(widget, title, file_filter)
        )
        layout.addWidget(button)
        return row

    def _choose_file(self, widget: QLineEdit, title: str, file_filter: str) -> None:
        path, _ = QFileDialog.getOpenFileName(self, title, widget.text(), file_filter)
        if path:
            widget.setText(path)

    def _refresh_summary(self) -> None:
        self.summary.setText(
            f"저장소: {self.deployment.data_root}\n"
            f"Compose 환경: {self.deployment.env_file}\n"
            f"접속 주소: {self.deployment.public_url}"
        )

    def _certificate_paths(self) -> tuple[Path | None, Path | None]:
        certificate = self.tls_certificate.text().strip()
        private_key = self.tls_private_key.text().strip()
        if certificate or private_key:
            return (
                Path(certificate).expanduser() if certificate else None,
                Path(private_key).expanduser() if private_key else None,
            )
        values = read_deployment_env(self.deployment.env_file)
        root_value = values.get("CERTS_DIR")
        if not root_value:
            return None, None
        root = deployment_path(self.server_dir, root_value)
        return root / "tls.crt", root / "tls.key"

    def _settings(self) -> NetworkSettings:
        scheme = self.scheme.currentData()
        return NetworkSettings(
            public_scheme=scheme,
            public_base_url=self.public_url.text().strip(),
            public_bind_address=self.public_bind.text().strip(),
            public_http_port=self.http_port.value(),
            public_https_port=self.https_port.value(),
            rtsp_bind_address=self.rtsp_bind.text().strip(),
            rtsp_port=self.rtsp_port.value(),
            allow_insecure_http=scheme == "http",
            cookie_secure=scheme == "https",
            nginx_config_file=Path(),
        )

    def _load_network(self) -> None:
        settings = load_network_settings(self.deployment.env_file, self.server_dir)
        self.scheme.setCurrentIndex(max(0, self.scheme.findData(settings.public_scheme)))
        self.public_url.setText(settings.public_base_url)
        self.public_bind.setText(settings.public_bind_address)
        self.http_port.setValue(settings.public_http_port)
        self.https_port.setValue(settings.public_https_port)
        self.rtsp_bind.setText(settings.rtsp_bind_address)
        self.rtsp_port.setValue(settings.rtsp_port)
        certificate, private_key = self._certificate_paths()
        self.tls_certificate.setText(str(certificate) if certificate else "")
        self.tls_private_key.setText(str(private_key) if private_key else "")

    def _validate_network(self) -> None:
        try:
            certificate, key = self._certificate_paths()
            validate_network_settings(
                self._settings(), cert_path=certificate, key_path=key
            )
        except (OSError, ValueError) as exc:
            self.output.setPlainText(f"네트워크 설정 오류: {exc}")
        else:
            self.output.setPlainText("네트워크 설정이 유효합니다.")

    def _set_busy(self, busy: bool) -> None:
        for button in (*self.action_buttons, *self.network_buttons):
            button.setEnabled(not busy)

    def _run(self, operation, completed, description: str) -> None:
        if self._task is not None:
            return
        self._set_busy(True)
        self.activity.setText(description)
        task = BackgroundTask(operation, self)
        self._task = task
        task.progress.connect(self.activity.setText)
        task.result.connect(completed)
        task.failed.connect(lambda message: self.output.setPlainText(message))

        def finished() -> None:
            self._task = None
            self._set_busy(False)
            self.activity.setText("작업 완료")
            task.deleteLater()

        task.finished.connect(finished)
        task.start()

    def _service_action(self, action: str) -> None:
        deployment = self.deployment
        self._run(
            lambda progress: run_service_action(
                self.server_dir, deployment, action, progress
            ),
            self.output.setPlainText,
            "서버 작업을 실행하고 있습니다…",
        )

    def _doctor(self) -> None:
        deployment = self.deployment

        def operation(progress):
            progress("배포 파일과 서비스 상태를 진단하고 있습니다…")
            return checks(
                self.server_dir,
                deployment.config_file,
                env_file=deployment.env_file,
            )

        def completed(results) -> None:
            self.output.setPlainText(
                "\n".join(
                    f"[{item.status}] {item.name}: {item.message}" for item in results
                )
            )

        self._run(operation, completed, "통합 진단을 실행하고 있습니다…")

    def _apply_network(self) -> None:
        try:
            settings = self._settings()
            certificate, key = self._certificate_paths()
            validate_network_settings(settings, cert_path=certificate, key_path=key)
        except (OSError, ValueError) as exc:
            self.output.setPlainText(f"네트워크 설정 오류: {exc}")
            return
        deployment = self.deployment

        def operation(progress):
            progress("네트워크 설정을 저장하고 서버를 재구성하고 있습니다…")
            return apply_network_settings(
                settings,
                deployment,
                self.server_dir,
                progress,
                cert_path=certificate,
                key_path=key,
            )

        def completed(message: str) -> None:
            self.output.setPlainText(message)
            self.deployment = inspect_installation(self.data_root, self.server_dir)
            self._load_network()
            self._refresh_summary()

        self._run(operation, completed, "네트워크 설정을 적용하고 있습니다…")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AI CCTV 서버 관리자")
    parser.add_argument("--server-dir", type=Path, default=default_server_dir())
    parser.add_argument("--data-root", type=Path, default=default_data_root())
    args = parser.parse_args(argv)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    try:
        window = ServerManagerWindow(
            server_dir=args.server_dir, data_root=args.data_root
        )
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    window.show()
    return app.exec_()
