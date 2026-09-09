# 설치된 중앙 서버에 Edge를 처음 연결하는 화면이며, 네트워크 요청은 입력 스냅샷으로 실행한다.
"""First Edge connection, kept separate from server installation and operation."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .edge_discovery import DiscoveredEdge, discover_edges
from .edge_pairing import (
    EdgePairingError,
    complete_edge_pairing,
    probe_edge_connection,
    validate_pairing_settings,
)
from .qt_tasks import BackgroundTask
from .server_api import (
    ServerApiClient,
    ServerApiError,
    prepare_private_output,
    write_publish_credentials,
)


# 서버 응답이나 예외 문자열을 그대로 보여 주지 않고 오류 종류별 고정 안내를 선택한다.
def _connection_error(error: Exception) -> str:
    """Server responses and arbitrary exception text may contain credentials."""

    if isinstance(error, ServerApiError):
        if error.status_code in {401, 403}:
            return "관리자 계정과 암호, 관리자 권한을 확인한 뒤 다시 연결하세요."
        if error.status_code == 409:
            return (
                "이미 등록된 카메라이거나 현재 등록할 수 없는 상태입니다. "
                "서버 관리자 화면에서 등록 여부를 확인하세요."
            )
        if error.status_code in {400, 422}:
            return "카메라 ID와 고급 설정의 장치 주소를 확인한 뒤 다시 연결하세요."
        if error.status_code == 429:
            return "요청이 많아 서버가 잠시 제한했습니다. 잠시 후 다시 시도하세요."
        return (
            "중앙 서버 응답을 확인하지 못했습니다. 서버 주소·인증서·연결을 확인하고, "
            "다시 등록하기 전에 서버 관리자 화면에서 카메라 등록 여부를 확인하세요."
        )
    if isinstance(error, EdgePairingError):
        return "Edge 응답을 확인하지 못했습니다. 장치의 연결 대기 상태와 LAN 연결을 확인하세요."
    if isinstance(error, OSError):
        return "네트워크 연결과 전달 파일 폴더의 쓰기 권한을 확인한 뒤 다시 시도하세요."
    return "입력값과 연결 상태를 확인한 뒤 다시 시도하세요."


# 서버 설치 이후의 장치 검색·응답 확인·첫 등록을 관리하며 작업 중 상태를 부모 화면에 알린다.
class EdgePanel(QWidget):
    busy_changed = pyqtSignal(bool)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._task: BackgroundTask | None = None
        self._action = ""
        self._data_root = Path.cwd()
        self._default_handoff = ""
        self._context: tuple[str, str, int, Path] | None = None
        self.discovered_edges: list[DiscoveredEdge] = []

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Pi에서 연결 대기 모드를 켠 뒤, 같은 연결 키로 장치를 찾으세요.\n"
            "장치를 선택하고 카메라 이름을 입력한 뒤 연결하면 설정을 전달합니다."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.controls = QWidget()
        controls_layout = QVBoxLayout(self.controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        self.username = QLineEdit("admin")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.edge_auth_token = QLineEdit()
        self.edge_auth_token.setEchoMode(QLineEdit.Password)
        self.edge_auth_token.setPlaceholderText("Pi에 설정한 32자 이상의 연결 키")
        self.discovered_edge_items = QComboBox()
        self.discovered_edge_items.setPlaceholderText("아직 찾은 장치가 없습니다")
        self.discovered_edge_items.currentIndexChanged.connect(
            self.select_discovered_edge
        )
        self.edge_name = QLineEdit()
        self.edge_name.setPlaceholderText("예: 현관, 실험실 입구")
        form.addRow("관리자 계정", self.username)
        form.addRow("관리자 암호", self.password)
        form.addRow("장치 연결 키", self.edge_auth_token)
        form.addRow("찾은 Edge", self.discovered_edge_items)
        form.addRow("카메라 이름", self.edge_name)
        controls_layout.addLayout(form)
        buttons = QHBoxLayout()
        self.discover_button = QPushButton("Edge 찾기")
        self.discover_button.clicked.connect(self.discover_edge_devices)
        self.test_button = QPushButton("연결 확인")
        self.test_button.clicked.connect(self.test_selected_edge_connection)
        self.connect_button = QPushButton("선택한 Edge 연결")
        self.connect_button.clicked.connect(self.register_edge)
        for button in (self.discover_button, self.test_button, self.connect_button):
            buttons.addWidget(button)
        controls_layout.addLayout(buttons)

        self.advanced_toggle = QToolButton()
        self.advanced_toggle.setText("고급 설정")
        self.advanced_toggle.setCheckable(True)
        self.advanced_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.advanced_toggle.setArrowType(Qt.RightArrow)
        self.advanced_toggle.toggled.connect(self._toggle_advanced)
        controls_layout.addWidget(self.advanced_toggle)
        self.advanced = QWidget()
        advanced_form = QFormLayout(self.advanced)
        self.manual_mode = QCheckBox("주소를 직접 입력해 등록")
        self.manual_mode.toggled.connect(self._manual_changed)
        self.server_url = QLineEdit()
        self.central_rtsp_host = QLineEdit()
        self.rtsp_port = QSpinBox()
        self.rtsp_port.setRange(1, 65535)
        self.rtsp_port.setValue(8554)
        self.edge_camera_id = QLineEdit("cam-001")
        self.edge_device_id = QLineEdit("edge-001")
        self.edge_management_url = QLineEdit()
        self.edge_management_url.setPlaceholderText("http://192.168.0.41:8003")
        self.edge_recovery_url = QLineEdit()
        self.edge_recovery_url.setPlaceholderText("http://192.168.0.41:8002")
        self.edge_backup_root = QLineEdit("/var/lib/ai-cctv-edge/recordings")
        self.publish_credentials_output = QLineEdit()
        self.edge_camera_id.textChanged.connect(self._update_handoff)
        self.video_profile = QComboBox()
        self.video_profile.addItems(["hd", "fhd"])
        self.choose_output_button = QPushButton("파일 위치 선택")
        self.choose_output_button.clicked.connect(
            self.choose_publish_credentials_output
        )
        advanced_form.addRow(self.manual_mode)
        advanced_form.addRow("중앙 서버 주소", self.server_url)
        advanced_form.addRow("Edge에서 접근할 서버 IP", self.central_rtsp_host)
        advanced_form.addRow("영상 수신 포트", self.rtsp_port)
        advanced_form.addRow("카메라 ID", self.edge_camera_id)
        advanced_form.addRow("장치 ID", self.edge_device_id)
        advanced_form.addRow("Edge 관리 주소", self.edge_management_url)
        advanced_form.addRow("Edge 복구 주소", self.edge_recovery_url)
        advanced_form.addRow("영상 화질", self.video_profile)
        advanced_form.addRow("Edge 백업 경로", self.edge_backup_root)
        advanced_form.addRow("수동 연결용 전달 파일", self.publish_credentials_output)
        advanced_form.addRow(self.choose_output_button)
        manual_note = QLabel(
            "직접 등록은 전달 파일을 Pi에 복사해 설정해야 합니다. "
            "관리·복구 주소는 중앙 서버에서 접근할 수 있어야 합니다."
        )
        manual_note.setWordWrap(True)
        advanced_form.addRow(manual_note)
        self.advanced.hide()
        controls_layout.addWidget(self.advanced)
        layout.addWidget(self.controls)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.hide()
        layout.addWidget(self.progress_bar)
        self.status = QLabel("Edge 찾기부터 시작하세요.")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        self.status.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.status)
        layout.addStretch()

    @property
    def busy(self) -> bool:
        return self._task is not None

    # 서버가 바뀌면 이전 장치와 비밀값을 지워 다른 배포에 잘못 연결하는 일을 막는다.
    def configure(
        self,
        public_url: str,
        rtsp_host: str,
        rtsp_port: int,
        data_root: Path,
        username: str = "admin",
    ) -> None:
        if self.busy:
            raise RuntimeError("Edge 연결 작업이 끝난 뒤 서버 설정을 바꿀 수 있습니다.")
        context = (public_url, rtsp_host, rtsp_port, Path(data_root))
        if self._context is not None and self._context != context:
            self.discovered_edges = []
            self.discovered_edge_items.clear()
            self.password.clear()
            self.edge_auth_token.clear()
            self.manual_mode.setChecked(False)
            self.edge_camera_id.setText("cam-001")
            self.edge_device_id.setText("edge-001")
            self.edge_management_url.clear()
            self.edge_recovery_url.clear()
            self.status.setText(
                "서버 설정이 바뀌었습니다. 연결할 Edge를 다시 찾으세요."
            )
        self._context = context
        self._data_root = Path(data_root)
        self.server_url.setText(public_url)
        self.central_rtsp_host.setText(rtsp_host)
        self.rtsp_port.setValue(rtsp_port)
        self.username.setText(username)
        self._update_handoff()

    def _toggle_advanced(self, checked: bool) -> None:
        self.advanced.setVisible(checked)
        self.advanced_toggle.setArrowType(Qt.DownArrow if checked else Qt.RightArrow)

    def _manual_changed(self, checked: bool) -> None:
        self.connect_button.setText(
            "입력한 Edge 등록" if checked else "선택한 Edge 연결"
        )

    # 자동 제안 경로만 카메라 ID에 맞춰 갱신하며 사용자가 지정한 인계 파일 위치는 유지한다.
    def _update_handoff(self) -> None:
        current = self.publish_credentials_output.text()
        if not current or current == self._default_handoff:
            camera_id = self.edge_camera_id.text().strip()
            # Invalid IDs must not choose a path outside the handoff directory.
            safe_id = (
                camera_id
                if camera_id
                and all(
                    char.isascii() and (char.isalnum() or char in "-_")
                    for char in camera_id
                )
                else "camera"
            )
            self._default_handoff = str(
                self._data_root / "secrets" / f"{safe_id}-publish-credentials.json"
            )
            self.publish_credentials_output.setText(self._default_handoff)

    def choose_publish_credentials_output(self) -> None:
        filename, _ = QFileDialog.getSaveFileName(
            self,
            "수동 연결용 전달 파일",
            self.publish_credentials_output.text(),
            "JSON 파일 (*.json)",
        )
        if filename:
            self.publish_credentials_output.setText(filename)

    # 잘못된 입력이 고급 설정 안에 있으면 먼저 펼친 뒤 해당 필드로 초점을 옮긴다.
    def _invalid(self, widget: QWidget, message: str) -> None:
        if self.advanced.isAncestorOf(widget):
            self.advanced_toggle.setChecked(True)
        widget.setFocus()
        self.status.setText(message)

    # 네트워크 작업을 QThread에서 실행하고 완료 신호가 올 때까지 중복 클릭과 설정 변경을 잠근다.
    def _start(self, action: str, operation: Callable) -> None:
        if self.busy:
            return
        self._action = action
        task = BackgroundTask(operation, self)
        self._task = task
        task.progress.connect(self.status.setText)
        task.result.connect(self._completed)
        task.failed.connect(self.status.setText)
        task.finished.connect(self._finished)
        self.controls.setEnabled(False)
        self.progress_bar.show()
        self.busy_changed.emit(True)
        task.start()

    # 스레드 종료 후 입력을 풀고 등록에 사용한 암호·연결 키를 지운 다음 Qt 객체를 정리한다.
    def _finished(self) -> None:
        task = self._task
        self._task = None
        self.controls.setEnabled(True)
        self.progress_bar.hide()
        if self._action == "register":
            self.password.clear()
            self.edge_auth_token.clear()
        self.busy_changed.emit(False)
        if task is not None:
            task.deleteLater()

    # 작업 결과를 GUI 스레드에서 반영하고 검색 결과가 있으면 첫 장치의 설정을 채운다.
    def _completed(self, result: Any) -> None:
        if self._action == "discover":
            self.discovered_edges = result
            self.discovered_edge_items.clear()
            for edge in result:
                self.discovered_edge_items.addItem(f"{edge.device_id} · {edge.address}")
            if not result:
                self.status.setText(
                    "장치를 찾지 못했습니다. Pi의 연결 대기 모드, 같은 연결 키, "
                    "같은 LAN과 방화벽의 UDP 37020 허용 여부를 확인하세요."
                )
                return
            self.discovered_edge_items.setCurrentIndex(0)
            self.select_discovered_edge(0)
            self.status.setText(
                f"Edge {len(result)}대를 찾았습니다. 장치를 선택해 연결하세요."
            )
        else:
            self.status.setText(result)

    # 연결 키를 UI에서 읽어 고정한 뒤 백그라운드에서 제한 시간 동안 LAN 광고를 수집한다.
    def discover_edge_devices(self) -> None:
        if self.busy:
            return
        key = self.edge_auth_token.text()
        if len(key) < 32 or key != key.strip():
            self._invalid(
                self.edge_auth_token, "Pi와 같은 32자 이상의 연결 키를 입력하세요."
            )
            return

        def discover(progress):
            progress("같은 LAN에서 연결 대기 중인 Edge를 찾는 중입니다…")
            try:
                return discover_edges(key, timeout=3.0)
            except Exception as exc:
                raise RuntimeError(
                    "장치 검색을 시작하지 못했습니다. 네트워크와 방화벽을 확인하세요."
                ) from exc

        self._start("discover", discover)

    # 선택한 광고의 장치 ID·접속 주소·지원 화질을 함께 반영한다.
    def select_discovered_edge(self, index: int) -> None:
        if index < 0 or index >= len(self.discovered_edges):
            return
        edge = self.discovered_edges[index]
        self.edge_device_id.setText(edge.device_id)
        self.edge_camera_id.setText(edge.camera_id)
        self.edge_management_url.setText(edge.management_url)
        self.edge_recovery_url.setText(edge.recovery_url)
        self.video_profile.clear()
        self.video_profile.addItems(list(edge.supported_profiles))

    # 입력값이 광고와 달라지면 자동 페어링 대상으로 인정하지 않아 수동 등록 선택을 요구한다.
    def selected_discovered_edge(self) -> DiscoveredEdge | None:
        index = self.discovered_edge_items.currentIndex()
        if self.manual_mode.isChecked() or not 0 <= index < len(self.discovered_edges):
            return None
        edge = self.discovered_edges[index]
        if (
            edge.device_id != self.edge_device_id.text().strip()
            or edge.camera_id != self.edge_camera_id.text().strip()
            or edge.management_url != self.edge_management_url.text().strip()
            or edge.recovery_url != self.edge_recovery_url.text().strip()
        ):
            return None
        return edge

    # 광고한 식별자와 실제 응답이 맞는지만 확인하며 서버 등록이나 설정 변경은 하지 않는다.
    def test_selected_edge_connection(self) -> None:
        if self.busy:
            return
        edge = self.selected_discovered_edge()
        if edge is None:
            self._invalid(
                self.discovered_edge_items, "먼저 Edge를 찾고 장치를 선택하세요."
            )
            return

        def probe(progress):
            progress("선택한 Edge의 응답과 장치 정보를 확인하는 중입니다…")
            try:
                probe_edge_connection(edge)
            except Exception as exc:
                raise RuntimeError(_connection_error(exc)) from exc
            return "Edge 응답과 장치 정보가 일치합니다. 이제 선택한 Edge를 연결할 수 있습니다."

        self._start("probe", probe)

    # UI 입력을 검증·복사한 뒤 중앙 등록과 Edge 설정 전달을 수행하고 수동 인계가 필요하면 계정을 저장한다.
    def register_edge(self) -> None:
        if self.busy:
            return
        edge = self.selected_discovered_edge()
        if edge is None and not self.manual_mode.isChecked():
            self._invalid(
                self.discovered_edge_items,
                "Edge를 찾아 선택하세요. 주소를 수정했다면 고급 설정에서 직접 등록을 선택하세요.",
            )
            return
        for widget, message in (
            (self.username, "관리자 계정을 입력하세요."),
            (self.password, "관리자 암호를 입력하세요."),
            (self.edge_name, "카메라 이름을 입력하세요."),
            (self.server_url, "고급 설정에서 중앙 서버 주소를 확인하세요."),
            (self.edge_camera_id, "카메라 ID를 입력하세요."),
            (self.edge_device_id, "장치 ID를 입력하세요."),
            (self.edge_management_url, "Edge 관리 주소를 입력하세요."),
            (self.edge_recovery_url, "Edge 복구 주소를 입력하세요."),
            (
                self.publish_credentials_output,
                "수동 연결용 전달 파일 위치를 지정하세요.",
            ),
        ):
            if not widget.text().strip():
                self._invalid(widget, message)
                return
        token = self.edge_auth_token.text()
        if (
            len(token) < 32
            or token != token.strip()
            or any(ord(char) < 0x20 or ord(char) == 0x7F for char in token)
        ):
            self._invalid(
                self.edge_auth_token, "Pi와 같은 32자 이상의 연결 키를 입력하세요."
            )
            return
        # Snapshot every widget before starting work; worker code uses only values.
        server_url = self.server_url.text().strip()
        username, password = self.username.text().strip(), self.password.text()
        camera_id = self.edge_camera_id.text().strip()
        registration = {
            "camera_id": camera_id,
            "name": self.edge_name.text().strip(),
            "edge_device_id": self.edge_device_id.text().strip(),
            "edge_management_url": self.edge_management_url.text().strip(),
            "edge_recovery_url": self.edge_recovery_url.text().strip(),
            "edge_auth_token": token,
        }
        central_host = self.central_rtsp_host.text().strip()
        central_port = self.rtsp_port.value()
        profile = self.video_profile.currentText()
        backup_root = self.edge_backup_root.text().strip()
        output = Path(self.publish_credentials_output.text())
        if edge is not None:
            try:
                central_host = validate_pairing_settings(
                    edge,
                    central_host=central_host,
                    central_port=central_port,
                    video_profile=profile,
                    backup_root=backup_root,
                )
            except EdgePairingError as exc:
                widget, message = {
                    "central_host": (
                        self.central_rtsp_host,
                        "Edge에서 접속할 서버 IP 또는 호스트 이름을 입력하세요. "
                        "0.0.0.0, :: 또는 https:// 주소는 사용할 수 없습니다.",
                    ),
                    "central_port": (
                        self.rtsp_port,
                        "영상 수신 포트는 1~65535 사이로 지정하세요.",
                    ),
                    "video_profile": (
                        self.video_profile,
                        "선택한 Edge가 지원하는 영상 화질을 선택하세요.",
                    ),
                    "backup_root": (
                        self.edge_backup_root,
                        "Edge 백업 경로는 /로 시작하는 Pi의 절대 경로를 입력하세요. "
                        "예: /var/lib/ai-cctv-edge/recordings",
                    ),
                }[exc.field]
                self._invalid(widget, message)
                return

        def register(progress):
            registered = False
            try:
                progress("전달 파일 위치와 중앙 서버 로그인을 확인하는 중입니다…")
                handoff = prepare_private_output(output)
                client = ServerApiClient(server_url)
                client.login(username, password)
                progress("중앙 서버에 Edge와 카메라를 등록하는 중입니다…")
                response = client.register_edge(**registration)
                # 이 시점 이후에는 중앙 등록이 끝났으므로 실패해도 같은 카메라를 다시 등록하면 안 된다.
                registered = True
                if edge is not None:
                    progress("Edge에 영상 연결 설정을 전달하는 중입니다…")
                    try:
                        complete_edge_pairing(
                            edge,
                            pairing_key=token,
                            server_response=response,
                            central_host=central_host,
                            central_port=central_port,
                            video_profile=profile,
                            backup_root=backup_root,
                        )
                    except EdgePairingError:
                        pass
                    else:
                        return (
                            "Edge 연결이 완료되었습니다. 서버 관리자 화면에서 장치 상태를, "
                            "앱에서 실제 영상을 확인하세요."
                        )
                # 수동 등록 또는 자동 전달 실패 시 일회성 계정을 파일에 남겨 Pi에서 이어서 적용하게 한다.
                write_publish_credentials(response, camera_id, handoff)
                return (
                    "중앙 등록이 완료되었습니다. 전달 파일을 해당 Pi로 옮겨 "
                    "ai-cctv-edge setup --publish-credentials-file <파일>로 적용하세요.\n"
                    f"전달 파일: {handoff}\n"
                    "이미 등록된 카메라를 다시 등록하지 마세요."
                )
            except Exception as exc:
                if registered:
                    raise RuntimeError(
                        "중앙 카메라는 등록되었지만 설정 전달을 완료하지 못했습니다. "
                        "서버 관리자 화면에서 게시 계정을 재발급해 Pi에 적용하세요. "
                        "카메라를 다시 등록하지 마세요."
                    ) from exc
                raise RuntimeError(_connection_error(exc)) from exc

        self._start("register", register)

    # 요청 중인 QThread가 화면보다 오래 남지 않도록 연결 작업 종료 전에는 닫기를 보류한다.
    def closeEvent(self, event) -> None:
        if self.busy:
            self.status.setText("연결 작업이 끝난 뒤 이 화면을 닫을 수 있습니다.")
            event.ignore()
        else:
            super().closeEvent(event)
