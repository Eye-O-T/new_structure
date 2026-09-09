"""처음에는 설치 마법사, 다시 열면 저장된 배포의 관리 화면을 제공한다."""

from __future__ import annotations

import os
import webbrowser
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

from PyQt5.QtCore import QSettings, QTimer, Qt
from PyQt5.QtNetwork import QAbstractSocket, QNetworkInterface
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ai_cctv_core.config import CameraBootstrap
from server.setup.config_core import (
    InstallRequest,
    _validate_public_base_url,
    _validate_request,
)
from server.setup.model_manager import IDENTITY_MODEL_NAME, resolve_identity_model

from .compose_adapter import (
    default_data_root,
    default_server_dir,
)
from .edge_panel import EdgePanel
from .host_actions import run_service_action
from .qt_tasks import BackgroundTask
from .workflow import Installation, inspect_installation, install_new, preflight


def local_networks() -> list[tuple[str, str]]:
    """외부 서버에 접속하지 않고 실행 중인 IPv4 인터페이스를 찾는다."""
    found = {}
    for interface in QNetworkInterface.allInterfaces():
        flags = interface.flags()
        if (
            not flags & QNetworkInterface.IsUp
            or not flags & QNetworkInterface.IsRunning
            or flags & QNetworkInterface.IsLoopBack
        ):
            continue
        for entry in interface.addressEntries():
            address = entry.ip()
            if address.protocol() != QAbstractSocket.IPv4Protocol:
                continue
            value = address.toString()
            parsed = ip_address(value)
            if parsed.is_link_local or parsed.is_unspecified or parsed.is_loopback:
                continue
            found[value] = (f"{interface.humanReadableName()} — {value}", value)
    choices = sorted(
        found.values(),
        key=lambda item: any(
            word in item[0].lower()
            for word in ("virtual", "vethernet", "docker", "wsl", "vpn")
        ),
    )
    return [*choices, ("이 PC에서만 사용 — 127.0.0.1", "127.0.0.1")]


# 저장소 상태에 따라 신규 설치 세 단계와 기존 설치 관리 화면을 선택하는 Qt 창이다.
class InstallerWindow(QWidget):
    # 명시한 저장소·환경 변수·최근 선택 순서로 위치를 복원하고 첫 진단은 이벤트 루프 시작 뒤 실행한다.
    def __init__(
        self, *, server_dir=None, data_root=None, preferences=None, auto_check=True
    ):
        super().__init__()
        self.server_dir = Path(server_dir or default_server_dir()).resolve()
        self.preferences = (
            preferences
            if preferences is not None
            else QSettings("AI CCTV", "Server Install Helper")
        )
        root = (
            data_root
            or os.getenv("AI_CCTV_DATA_ROOT")
            or self.preferences.value("storage_root")
            or default_data_root()
        )
        self.installation: Installation | None = None
        self._task = None
        self._busy = False
        self._preflight_ok = False
        self._request = None
        self._notice = None
        self._location_error = False
        self._last_suggested_url = ""
        self._after_task = None
        self.setWindowTitle("AI CCTV 서버 설치 도우미")
        self.resize(800, 780)
        self.setMinimumSize(640, 600)
        self.setStyleSheet(
            "QLabel#title {font-size:20px;font-weight:600;} QPushButton {padding:7px 14px;} QLineEdit,QComboBox,QSpinBox {min-height:26px;}"
        )
        layout = QVBoxLayout(self)
        self.title = QLabel("AI CCTV 서버 설치")
        self.title.setObjectName("title")
        layout.addWidget(self.title)
        location = QHBoxLayout()
        location.addWidget(QLabel("저장 위치"))
        self.storage = QLineEdit(str(Path(root).expanduser().resolve()))
        self.storage.setReadOnly(True)
        location.addWidget(self.storage, 1)
        self.choose_storage_button = QPushButton("변경…")
        self.choose_storage_button.clicked.connect(self.choose_storage)
        location.addWidget(self.choose_storage_button)
        layout.addLayout(location)
        self.steps = QLabel()
        layout.addWidget(self.steps)
        self.pages = QStackedWidget()
        layout.addWidget(self.pages, 1)
        self._build_prerequisites()
        self._build_settings()
        self._build_review()
        self._build_management()
        self.activity = QLabel()
        self.activity.setWordWrap(True)
        layout.addWidget(self.activity)
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        layout.addWidget(self.progress)
        navigation = QHBoxLayout()
        self.back_button = QPushButton("이전")
        self.back_button.clicked.connect(self.go_back)
        self.next_button = QPushButton("다음")
        self.next_button.clicked.connect(self.go_next)
        self.close_button = QPushButton("닫기")
        self.close_button.clicked.connect(self.close)
        navigation.addWidget(self.back_button)
        navigation.addStretch()
        navigation.addWidget(self.next_button)
        navigation.addWidget(self.close_button)
        layout.addLayout(navigation)
        self._load_location()
        if auto_check:
            QTimer.singleShot(0, self.check_current_location)

    # 창 높이가 작아도 모든 설정에 접근할 수 있도록 각 단계를 스크롤 가능한 페이지로 만든다.
    def _page(self, title, description):
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        layout = QVBoxLayout(content)
        heading = QLabel(title)
        heading.setStyleSheet("font-size:17px;font-weight:600")
        layout.addWidget(heading)
        label = QLabel(description)
        label.setWordWrap(True)
        layout.addWidget(label)
        scroll.setWidget(content)
        self.pages.addWidget(scroll)
        return layout

    # 파일 선택과 직접 경로 입력을 묶고 경로가 바뀌면 이전 사전 검사 통과 상태를 무효화한다.
    def _file_row(self, form, label, widget, file_filter):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(widget, 1)
        button = QPushButton("파일 선택…")
        button.clicked.connect(lambda: self._choose_file(widget, label, file_filter))
        layout.addWidget(button)
        form.addRow(label, row)
        widget.textChanged.connect(self._invalidate_preflight)

    # 모델·TLS 파일 선택과 Docker 준비 결과를 첫 단계에서 함께 확인하게 한다.
    def _build_prerequisites(self):
        layout = self._page(
            "1. 설치 준비 확인",
            "필수 프로그램과 파일을 먼저 확인합니다. 빠진 항목을 준비한 뒤 ‘다시 검사’를 누르세요. 검사에 통과하면 기본값으로 설치를 계속할 수 있습니다.",
        )
        self.model = QLineEdit()
        self.identity_model = QLineEdit()
        self.tls_certificate = QLineEdit()
        self.tls_private_key = QLineEdit()
        files = QFormLayout()
        self._file_row(
            files, "사람 탐지 모델", self.model, "모델 파일 (*.pt *.onnx *.engine)"
        )
        self._file_row(
            files, "인물 식별 모델(OSNet)", self.identity_model, "ONNX 모델 (*.onnx)"
        )
        self._file_row(
            files, "HTTPS 인증서", self.tls_certificate, "인증서 (*.crt *.pem)"
        )
        self._file_row(
            files, "인증서 개인키", self.tls_private_key, "개인키 (*.key *.pem)"
        )
        layout.addLayout(files)
        hint = QLabel(
            "Docker Desktop을 설치하고 Linux 컨테이너 모드로 실행해 주세요. 모델과 HTTPS 인증서·개인키는 배포 담당자에게 받은 파일을 선택합니다. 인증서는 사용할 서버 주소와 일치하고 PC·휴대전화에서 신뢰되어야 합니다."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.check_results = QTextEdit()
        self.check_results.setReadOnly(True)
        self.check_results.setMinimumHeight(180)
        layout.addWidget(self.check_results, 1)
        self.check_button = QPushButton("다시 검사")
        self.check_button.clicked.connect(self.check_prerequisites)
        layout.addWidget(self.check_button)

    @staticmethod
    def _spin(value, minimum, maximum):
        widget = QSpinBox()
        widget.setRange(minimum, maximum)
        widget.setValue(value)
        return widget

    # 기본 접속·관리자 설정과 고급 녹화·추론 설정을 구성하며 접힌 고급 입력값도 유지한다.
    def _build_settings(self):
        layout = self._page(
            "2. 기본 설정",
            "관리자 비밀번호와 접속할 네트워크를 확인하세요. 나머지 값은 기본값으로 사용할 수 있습니다.",
        )
        form = QFormLayout()
        self.username = QLineEdit("admin")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.password.setPlaceholderText("12자 이상")
        self.confirm_password = QLineEdit()
        self.confirm_password.setEchoMode(QLineEdit.Password)
        self.network = QComboBox()
        for label, value in local_networks():
            self.network.addItem(label, value)
        self.public_base_url = QLineEdit()
        self.public_base_url.setPlaceholderText("예: https://cctv.example.com")
        form.addRow("관리자 계정", self.username)
        form.addRow("비밀번호", self.password)
        form.addRow("비밀번호 확인", self.confirm_password)
        form.addRow("카메라·휴대전화 연결망", self.network)
        form.addRow("휴대전화에서 접속할 서버 주소", self.public_base_url)
        layout.addLayout(form)
        label = QLabel(
            "접속 주소는 인증서에 등록된 이름 또는 IP와 같아야 합니다. ‘이 PC에서만 사용’을 선택하면 외부 카메라와 휴대전화는 연결할 수 없습니다."
        )
        label.setWordWrap(True)
        layout.addWidget(label)
        self.advanced_toggle = QCheckBox("고급 설정 직접 지정")
        self.advanced_toggle.setToolTip(
            "접어도 변경한 값은 유지됩니다. 마지막 설치 내용 확인에서 적용할 값을 확인하세요."
        )
        layout.addWidget(self.advanced_toggle)
        self.advanced = QGroupBox("고급 설정")
        advanced = QFormLayout(self.advanced)
        self.http = self._spin(80, 1, 65535)
        self.https = self._spin(443, 1, 65535)
        self.rtsp_port = self._spin(8554, 1, 65535)
        self.public_bind = QLineEdit()
        self.rtsp_bind = QLineEdit()
        self.inference_device = QComboBox()
        self.inference_device.setEditable(True)
        self.inference_device.addItems(["auto", "cpu", "cuda:0"])
        self.recording_segment_seconds = self._spin(60, 10, 300)
        self.retention_days = self._spin(7, 1, 36500)
        self.storage_warning_free_percent = self._spin(15, 1, 99)
        self.cameras = QLineEdit()
        self.cameras.setPlaceholderText("선택 사항: cam-001:입구,cam-002:복도")
        for label, widget in (
            ("HTTP 포트", self.http),
            ("HTTPS 포트", self.https),
            ("영상 수신 RTSP 포트", self.rtsp_port),
            ("웹 수신 IP", self.public_bind),
            ("카메라 영상 수신 IP", self.rtsp_bind),
            ("추론 장치", self.inference_device),
            ("녹화 파일 길이(초)", self.recording_segment_seconds),
            ("녹화 보관 기간(일)", self.retention_days),
            ("남은 저장 공간 경고(%)", self.storage_warning_free_percent),
            ("초기 카메라 등록", self.cameras),
        ):
            advanced.addRow(label, widget)
        self.advanced.hide()
        self.advanced_toggle.toggled.connect(self.advanced.setVisible)
        layout.addWidget(self.advanced)
        layout.addStretch()
        self.network.currentIndexChanged.connect(self._network_changed)
        self.https.valueChanged.connect(self._suggest_url)
        self._network_changed()

    # 검증된 요청의 공개 설정을 설치 전에 확인할 읽기 전용 영역을 준비한다.
    def _build_review(self):
        layout = self._page(
            "3. 설치 내용 확인",
            "‘설치 및 시작’을 누르면 설정을 저장하고 서버를 실행합니다. 이미지 준비에는 인터넷 연결과 시간이 필요할 수 있습니다.",
        )
        self.review = QTextEdit()
        self.review.setReadOnly(True)
        layout.addWidget(self.review, 1)

    # 기존 설정으로 실행하는 서버 제어와 최초 카메라 연결을 서로 다른 탭에 배치한다.
    def _build_management(self):
        layout = self._page(
            "서버 관리",
            "저장된 설정으로 서버를 관리합니다. 창을 닫아도 실행 중인 서버는 계속 동작합니다.",
        )
        self.management_info = QLabel()
        self.management_info.setWordWrap(True)
        layout.addWidget(self.management_info)
        self.management_tabs = QTabWidget()
        layout.addWidget(self.management_tabs, 1)
        controls = QWidget()
        controls_layout = QVBoxLayout(controls)
        row = QHBoxLayout()
        self.service_buttons = []
        for label, action in (
            ("서버 시작 / 업데이트 적용", "start"),
            ("재시작", "restart"),
            ("중지", "stop"),
            ("상태 확인", "status"),
        ):
            button = QPushButton(label)
            button.clicked.connect(
                lambda _checked=False, selected=action: self.service_action(selected)
            )
            row.addWidget(button)
            self.service_buttons.append(button)
        controls_layout.addLayout(row)
        hint = QLabel(
            "시작은 저장된 설정으로 서버를 구성하고 업데이트를 적용합니다. 재시작은 현재 컨테이너를 다시 실행합니다. 초기 설정과 인증키는 다시 만들지 않습니다."
        )
        hint.setWordWrap(True)
        controls_layout.addWidget(hint)
        self.management_output = QTextEdit()
        self.management_output.setReadOnly(True)
        controls_layout.addWidget(self.management_output, 1)
        self.open_admin_button = QPushButton("서버 관리자 화면 열기")
        self.open_admin_button.clicked.connect(self.open_admin)
        controls_layout.addWidget(self.open_admin_button)
        self.management_tabs.addTab(controls, "서버 상태")
        self.edge_panel = EdgePanel()
        self.edge_panel.busy_changed.connect(self._edge_busy_changed)
        edge_scroll = QScrollArea()
        edge_scroll.setWidgetResizable(True)
        edge_scroll.setWidget(self.edge_panel)
        self.management_tabs.addTab(edge_scroll, "카메라 연결")

    # 선택한 네트워크 IP를 웹·RTSP 수신 기본값에 함께 반영한다.
    def _network_changed(self):
        address = self.network.currentData() or "127.0.0.1"
        self.public_bind.setText(address)
        self.rtsp_bind.setText(address)
        self._suggest_url()

    # 이전 자동 제안값일 때만 주소를 갱신해 사용자가 입력한 인증서 호스트 이름을 보존한다.
    def _suggest_url(self):
        host = self.network.currentData() or "127.0.0.1"
        port = self.https.value()
        suggestion = f"https://{host}" + (f":{port}" if port != 443 else "")
        if (
            not self.public_base_url.text()
            or self.public_base_url.text() == self._last_suggested_url
        ):
            self.public_base_url.setText(suggestion)
        self._last_suggested_url = suggestion

    def _choose_file(self, widget, title, file_filter):
        path, _ = QFileDialog.getOpenFileName(self, title, widget.text(), file_filter)
        if path:
            widget.setText(path)

    # 모델이나 TLS 경로 변경 후에는 다시 검사하기 전까지 다음 단계 진행을 막는다.
    def _invalidate_preflight(self):
        self._preflight_ok = False
        self._update_navigation()

    # 빈 입력만 채우며 모델 후보가 하나일 때만 자동 선택해 여러 모델 중 임의 선택을 피한다.
    def _discover_files(self):
        root = Path(self.storage.text())
        if not self.model.text():
            for directory in (root / "models", self.server_dir / "runtime" / "models"):
                if directory.is_dir():
                    matches = [
                        p
                        for p in directory.iterdir()
                        if p.is_file()
                        and p.suffix.lower() in {".pt", ".onnx", ".engine"}
                        and p.name != IDENTITY_MODEL_NAME
                    ]
                    if len(matches) == 1:
                        self.model.setText(str(matches[0]))
                        break
        if not self.identity_model.text():
            try:
                identity = resolve_identity_model(None, root, self.server_dir)
            except (OSError, ValueError):
                pass
            else:
                self.identity_model.setText(str(identity))
        for directory in (root / "certs", self.server_dir / "runtime" / "certificates"):
            if not self.tls_certificate.text() and (directory / "tls.crt").is_file():
                self.tls_certificate.setText(str(directory / "tls.crt"))
            if not self.tls_private_key.text() and (directory / "tls.key").is_file():
                self.tls_private_key.setText(str(directory / "tls.key"))

    # 완료된 설치·빈 저장소·복구가 필요한 저장소를 구분해 올바른 화면과 버튼 상태로 전환한다.
    def _load_location(self):
        self._location_error = False
        self.installation = None
        self._preflight_ok = False
        try:
            self.installation = inspect_installation(
                Path(self.storage.text()), self.server_dir
            )
        except (OSError, ValueError) as exc:
            self._location_error = True
            self.pages.setCurrentIndex(3)
            self.title.setText("기존 설치 확인 필요")
            self.management_info.setText(
                "기존 파일이 있어 새 설치로 덮어쓸 수 없습니다. 설정을 복구하거나 다른 저장 위치를 선택하세요."
            )
            self.management_output.setPlainText(str(exc))
        else:
            if self.installation is not None:
                self._show_management(self.installation)
            else:
                self.title.setText("AI CCTV 서버 설치")
                self.pages.setCurrentIndex(0)
                self._discover_files()
                self.check_results.setPlainText("필수 프로그램과 파일을 검사해 주세요.")
        self._update_navigation()

    # 새 저장 위치를 선택하면 기존 검사 결과를 버리고 해당 위치의 설치 상태부터 다시 읽는다.
    def choose_storage(self):
        path = QFileDialog.getExistingDirectory(
            self, "영상과 설정을 저장할 폴더", self.storage.text()
        )
        if path:
            self.storage.setText(str(Path(path).resolve()))
            self._load_location()
            self.check_current_location()

    # 저장된 설치는 서비스 상태를 조회하고 신규 위치는 설치 준비 파일을 검사한다.
    def check_current_location(self):
        if self._location_error:
            return
        if self.installation is not None:
            self.service_action("status")
        else:
            self.check_prerequisites()

    @staticmethod
    def _path_or_none(widget):
        text = widget.text().strip()
        return Path(text).expanduser() if text else None

    # 입력 경로를 먼저 복사해 백그라운드 검사에 넘기고 모든 항목 통과 시에만 진행을 허용한다.
    def check_prerequisites(self):
        if self._busy or self.installation or self._location_error:
            return
        self._preflight_ok = False
        args = (
            self.server_dir,
            self._path_or_none(self.model),
            self._path_or_none(self.tls_certificate),
            self._path_or_none(self.tls_private_key),
            self._path_or_none(self.identity_model),
            Path(self.storage.text()),
        )

        def completed(checks):
            self.check_results.setPlainText(
                "\n\n".join(
                    f"{'[확인]' if item.ok else '[준비 필요]'} {item.name}\n{item.message}"
                    for item in checks
                )
            )
            failed = [item for item in checks if not item.ok]
            self._preflight_ok = not failed
            if failed:
                self._show_notice(
                    "설치 준비가 필요합니다",
                    "\n".join(f"• {item.name}: {item.message}" for item in failed),
                )
            self.activity.setText(
                "준비 확인을 마쳤습니다. ‘다음’을 누르세요."
                if not failed
                else "준비되지 않은 항목을 해결한 뒤 다시 검사해 주세요."
            )

        self._start_task(
            lambda progress: preflight(*args),
            completed,
            "필수 프로그램과 파일을 검사하고 있습니다…",
        )

    # 모달 경고는 비동기로 열어 Qt 이벤트 루프와 진행 중인 작업의 완료 신호를 막지 않는다.
    def _show_notice(self, title, message):
        if self._notice is not None:
            self._notice.close()
        self._notice = QMessageBox(
            QMessageBox.Warning, title, message, QMessageBox.Ok, self
        )
        self._notice.setWindowModality(Qt.WindowModal)
        self._notice.open()

    # 단계·검사 통과·작업 중 여부를 한곳에서 반영해 잘못된 화면 전환이나 중복 실행을 막는다.
    def _update_navigation(self):
        if not hasattr(self, "next_button"):
            return
        page = self.pages.currentIndex()
        managing = page == 3
        self.steps.setText(
            "기존 설치 관리"
            if managing
            else f"{page + 1} / 3  ·  준비 확인 → 기본 설정 → 설치 및 실행"
        )
        self.back_button.setVisible(not managing)
        self.back_button.setEnabled(not self._busy and page > 0)
        self.next_button.setVisible(not managing)
        self.next_button.setText("설치 및 시작" if page == 2 else "다음")
        self.next_button.setEnabled(
            not self._busy
            and not self._location_error
            and (page != 0 or self._preflight_ok)
        )
        self.choose_storage_button.setEnabled(not self._busy)
        self.close_button.setEnabled(not self._busy)
        self.pages.setEnabled(not self._busy)
        if managing and not self._busy:
            for button in self.service_buttons:
                button.setEnabled(self.installation is not None)
            self.open_admin_button.setEnabled(
                bool(self.installation and self.installation.public_url)
            )
            self.management_tabs.setTabEnabled(1, self.installation is not None)

    # 카메라 연결 중에는 저장소 변경과 서버 제어를 막아 같은 설치를 대상으로 작업하게 한다.
    def _edge_busy_changed(self, busy):
        self._busy = busy
        self.choose_storage_button.setEnabled(not busy)
        self.close_button.setEnabled(not busy)
        self.management_tabs.setTabEnabled(0, not busy)

    def go_back(self):
        if not self._busy and self.pages.currentIndex() in (1, 2):
            self.pages.setCurrentIndex(self.pages.currentIndex() - 1)
            self._update_navigation()

    # 암호 확인과 HTTPS 주소·포트 일치를 검증하고 현재 화면 값을 공통 설치 요청으로 변환한다.
    def _make_request(self):
        if self.password.text() != self.confirm_password.text():
            raise ValueError("비밀번호와 비밀번호 확인이 일치하지 않습니다.")
        if len(self.password.text()) < 12:
            raise ValueError("관리자 비밀번호를 12자 이상 입력해 주세요.")
        url = _validate_public_base_url(self.public_base_url.text())
        if not url:
            raise ValueError("휴대전화에서 접속할 서버 주소를 입력해 주세요.")
        if (urlsplit(url).port or 443) != self.https.value():
            raise ValueError(
                "접속 주소의 포트와 고급 설정의 HTTPS 포트를 같게 지정해 주세요."
            )
        cameras = []
        for item in filter(None, map(str.strip, self.cameras.text().split(","))):
            camera_id, _, name = item.partition(":")
            cameras.append(CameraBootstrap(camera_id=camera_id, name=name or camera_id))
        request = InstallRequest(
            data_root=Path(self.storage.text()),
            server_dir=self.server_dir,
            admin_username=self.username.text().strip(),
            admin_password=self.password.text(),
            model_path=Path(self.model.text()),
            identity_model_path=self._path_or_none(self.identity_model),
            cameras=cameras,
            tls_certificate_path=Path(self.tls_certificate.text()),
            tls_private_key_path=Path(self.tls_private_key.text()),
            public_base_url=url,
            public_bind_address=self.public_bind.text().strip(),
            rtsp_bind_address=self.rtsp_bind.text().strip(),
            public_http_port=self.http.value(),
            public_https_port=self.https.value(),
            rtsp_port=self.rtsp_port.value(),
            inference_device=self.inference_device.currentText(),
            recording_segment_seconds=self.recording_segment_seconds.value(),
            retention_days=self.retention_days.value(),
            storage_warning_free_percent=self.storage_warning_free_percent.value(),
        )
        _validate_request(request)
        return request

    # 준비 검사와 입력 검증을 통과한 요청만 확인 화면으로 넘기고 마지막 단계에서 설치를 시작한다.
    def go_next(self):
        if self._busy:
            return
        page = self.pages.currentIndex()
        if page == 0 and self._preflight_ok:
            self.pages.setCurrentIndex(1)
        elif page == 1:
            try:
                self._request = self._make_request()
            except (OSError, ValueError) as exc:
                self._show_notice("설정 확인", str(exc))
                return
            request = self._request
            self.review.setPlainText(
                f"저장 위치: {request.data_root}\n관리자: {request.admin_username}\n"
                f"접속 주소: {request.public_base_url}\n웹 수신 IP: {request.public_bind_address}\n"
                f"카메라 영상 수신: {request.rtsp_bind_address}:{request.rtsp_port}\n"
                f"녹화: {request.recording_segment_seconds}초 단위 / {request.retention_days}일 보관\n"
                f"AI 모델: {request.model_path.name}\n추론 장치: {request.inference_device}\n\n"
                f"인물 식별 모델: {request.identity_model_path.name if request.identity_model_path else IDENTITY_MODEL_NAME}\n\n"
                "설정과 인증키 생성 → 모델·인증서 준비 → 서버 이미지 준비 → 서버 실행 순서로 진행합니다.\n"
                "초기 실행 후 ‘카메라 연결’에서 Edge를 연결하세요. 실제 영상과 감지 동작은 카메라 연결 후 확인합니다."
            )
            self.pages.setCurrentIndex(2)
        elif page == 2:
            self.begin_installation()
        self._update_navigation()

    # 설정 저장이 끝나면 관리 화면을 먼저 복원하고 스레드 종료 후 별도 작업으로 서버를 시작한다.
    def begin_installation(self):
        if self._busy or self.installation is not None:
            return
        request = self._request
        if request is None:
            return
        self.preferences.setValue("storage_root", str(request.data_root))
        self.preferences.sync()

        def install(progress):
            progress("설정·모델·인증서를 준비하고 있습니다…")
            install_new(request)
            installed = inspect_installation(request.data_root, self.server_dir)
            if installed is None:
                raise ValueError("설정 저장 결과를 확인할 수 없습니다.")
            return installed

        def configured(installed):
            self._show_management(installed)
            self.management_output.setPlainText(
                "설정 저장을 마쳤습니다. 서버를 시작합니다…"
            )
            # 설정 저장과 Docker 기동을 분리해 첫 기동이 실패해도 기존 설정으로 다시 시작할 수 있다.
            self._after_task = lambda: self.service_action("start")

        self._after_task = None
        self._start_task(
            install, configured, "설치를 준비하고 있습니다…", reload_after_failure=True
        )

    # 저장된 공개 정보로 관리 화면을 채우고 설치용 요청과 비밀번호 입력은 지운다.
    def _show_management(self, installed):
        self.installation = installed
        self._location_error = False
        self._request = None
        self.password.clear()
        self.confirm_password.clear()
        self.title.setText("AI CCTV 서버 관리")
        self.pages.setCurrentIndex(3)
        self.preferences.setValue("storage_root", str(installed.data_root))
        self.preferences.sync()
        self.management_info.setText(
            f"저장된 설정을 불러왔습니다.\n접속 주소: {installed.public_url or '설정되지 않음'}\n설정: {installed.env_file}"
        )
        self.edge_panel.configure(
            installed.public_url,
            installed.rtsp_host,
            installed.rtsp_port,
            installed.data_root,
            installed.admin_username,
        )
        self._update_navigation()

    # 스레드 수명과 busy 상태를 관리하며 필요하면 실패 후 저장소를 다시 읽어 중단 설치를 감지한다.
    def _start_task(
        self, operation, completed, description, *, reload_after_failure=False
    ):
        if self._busy:
            return
        self._busy = True
        self.activity.setText(description)
        self.progress.show()
        self._update_navigation()
        task = BackgroundTask(operation, self)
        self._task = task
        task.progress.connect(self.activity.setText)
        task.result.connect(completed)

        def failed(message):
            self.activity.setText("작업을 완료하지 못했습니다. 안내를 확인해 주세요.")
            self._show_notice("작업 확인 필요", message)
            if reload_after_failure:
                self._load_location()
            if self.pages.currentIndex() == 3:
                self.management_output.setPlainText(message)

        def finished():
            self._task = None
            self._busy = False
            self.progress.hide()
            task.deleteLater()
            continuation = self._after_task
            self._after_task = None
            if continuation:
                # 다음 작업을 바로 시작해 저장 위치 변경 이벤트가 사이에 끼지 않게 한다.
                continuation()
            else:
                self._update_navigation()

        task.failed.connect(failed)
        task.finished.connect(finished)
        task.start()

    # 현재 설치 정보를 고정해 호스트 작업에 넘긴다. 시작 재시도도 저장된 설정과 인증키를 사용한다.
    def service_action(self, action):
        if self._busy or self.installation is None:
            return
        installed = self.installation
        descriptions = {
            "start": "서버 이미지를 준비하고 실행하고 있습니다. 처음에는 시간이 걸릴 수 있습니다…",
            "restart": "저장된 서버를 재시작하고 있습니다…",
            "stop": "서버를 중지하고 있습니다…",
            "status": "서버 상태를 확인하고 있습니다…",
        }

        def operate(progress):
            return run_service_action(self.server_dir, installed, action, progress)

        def completed(output):
            self.management_output.setPlainText(output)
            if action == "start":
                self.activity.setText(
                    "서버 실행 명령을 마쳤습니다. 카메라 연결 후 영상·감지·녹화를 확인하세요."
                )
            else:
                self.activity.setText("요청한 작업을 마쳤습니다.")

        self._start_task(operate, completed, descriptions[action])

    def open_admin(self):
        if self.installation and self.installation.public_url:
            webbrowser.open(self.installation.public_url.rstrip("/") + "/admin/")

    # 설치 또는 Edge 작업 중에는 창 종료를 보류해 작업 객체가 실행 도중 파괴되지 않게 한다.
    def closeEvent(self, event):
        if self._busy or self.edge_panel.busy:
            self.activity.setText("진행 중인 작업이 끝난 뒤 창을 닫아 주세요.")
            event.ignore()
        else:
            event.accept()
