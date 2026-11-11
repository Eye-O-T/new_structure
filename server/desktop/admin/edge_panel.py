# 설치된 중앙 서버에 Edge를 처음 연결하는 화면이며, 네트워크 요청은 입력 스냅샷으로 실행한다.
"""First Edge connection, kept separate from server installation and operation."""

from __future__ import annotations

import re
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

from server.edge_onboarding.discovery import DiscoveredEdge
from server.edge_onboarding.host_helper import (
    discover_edges_via_helper,
    pair_edge_via_helper,
    probe_edge_via_helper,
)
from server.edge_onboarding.pairing import (
    EdgePairingError,
    validate_pairing_settings,
)
from server.desktop.common.tasks import BackgroundTask
from server.edge_onboarding.server_api import (
    prepare_private_output,
    write_publish_credentials,
)
from .api import DesktopApi


def _connection_error(error: Exception) -> str:
    """Show a useful error without echoing request payloads or credentials."""
    status_code = getattr(error, "status_code", None)
    code = getattr(error, "code", None)
    if status_code is not None or code:
        status = f"HTTP {status_code}" if status_code is not None else "요청 오류"
        return f"{status}{f' [{code}]' if code else ''}: {getattr(error, 'message', '')}"
    return str(error) or error.__class__.__name__


# 서버 설치 이후의 장치 검색·응답 확인·첫 등록을 관리하며 작업 중 상태를 부모 화면에 알린다.
class EdgePanel(QWidget):
    busy_changed = pyqtSignal(bool)
    registration_finished = pyqtSignal()

    def __init__(self, api: DesktopApi, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.api = api
        self._task: BackgroundTask | None = None
        self._action = ""
        self._data_root = Path.cwd()
        self._default_handoff = ""
        self._context: tuple[str, str, int, Path] | None = None
        self._pending_response: dict[str, Any] | None = None
        self._retry_context: dict[str, Any] | None = None
        self._uncertain_camera_id: str | None = None
        self.discovered_edges: list[DiscoveredEdge] = []

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Pi에서 Edge discovery 모드를 시작한 뒤 같은 LAN에서 장치를 찾으세요.\n"
            "검색에는 인증 토큰이 필요하지 않으며, 등록할 때 Edge 운영 인증 토큰을 입력합니다."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.controls = QWidget()
        controls_layout = QVBoxLayout(self.controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        self.edge_auth_token = QLineEdit()
        self.edge_auth_token.setEchoMode(QLineEdit.Password)
        self.edge_auth_token.setPlaceholderText("Pi에서 안전하게 전달받은 32자 이상의 운영 인증 토큰")
        token_row = QWidget()
        token_layout = QHBoxLayout(token_row)
        token_layout.setContentsMargins(0, 0, 0, 0)
        token_layout.addWidget(self.edge_auth_token, 1)
        token_file_button = QPushButton("토큰 파일 불러오기")
        token_file_button.clicked.connect(self.choose_edge_auth_token_file)
        token_layout.addWidget(token_file_button)
        self.discovered_edge_items = QComboBox()
        self.discovered_edge_items.setPlaceholderText("아직 찾은 장치가 없습니다")
        self.discovered_edge_items.currentIndexChanged.connect(
            self.select_discovered_edge
        )
        self.edge_name = QLineEdit()
        self.edge_name.setPlaceholderText("예: 현관, 실험실 입구")
        self.session_label = QLabel("관제 화면의 로그인 세션을 사용합니다.")
        form.addRow("중앙 서버 로그인", self.session_label)
        form.addRow("Edge 운영 인증 토큰", token_row)
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
        self.retry_button = QPushButton("Edge 설정 전달 재시도")
        self.retry_button.clicked.connect(self.retry_pairing)
        self.retry_button.hide()
        for button in (self.discover_button, self.test_button, self.connect_button, self.retry_button):
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
        self.server_url.setReadOnly(True)
        self.central_rtsp_host = QLineEdit()
        self.rtsp_port = QSpinBox()
        self.rtsp_port.setRange(1, 65535)
        self.rtsp_port.setValue(8554)
        self.edge_camera_id = QLineEdit("cam-001")
        self.edge_device_id = QLineEdit("edge-001")
        self.edge_mac_address = QLineEdit()
        self.edge_mac_address.setPlaceholderText("aa:bb:cc:dd:ee:ff")
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
        self.save_pending_button = QPushButton("게시 계정 파일 저장 재시도")
        self.save_pending_button.clicked.connect(self.save_pending)
        self.save_pending_button.setEnabled(False)
        advanced_form.addRow(self.manual_mode)
        advanced_form.addRow("중앙 서버 주소", self.server_url)
        advanced_form.addRow("Edge에서 접근할 서버 IP", self.central_rtsp_host)
        advanced_form.addRow("영상 수신 포트", self.rtsp_port)
        advanced_form.addRow("카메라 ID", self.edge_camera_id)
        advanced_form.addRow("장치 ID", self.edge_device_id)
        advanced_form.addRow("MAC Address", self.edge_mac_address)
        advanced_form.addRow("Edge 관리 주소", self.edge_management_url)
        advanced_form.addRow("Edge 복구 주소", self.edge_recovery_url)
        advanced_form.addRow("영상 화질", self.video_profile)
        advanced_form.addRow("Edge 백업 경로", self.edge_backup_root)
        advanced_form.addRow("수동 연결용 전달 파일", self.publish_credentials_output)
        advanced_form.addRow(self.choose_output_button)
        advanced_form.addRow(self.save_pending_button)
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

    @property
    def has_pending(self) -> bool:
        return self._pending_response is not None

    # 서버가 바뀌면 이전 장치와 비밀값을 지워 다른 배포에 잘못 연결하는 일을 막는다.
    def configure(
        self,
        public_url: str,
        rtsp_host: str,
        rtsp_port: int,
        data_root: Path,
    ) -> None:
        if self.busy:
            raise RuntimeError("Edge 연결 작업이 끝난 뒤 서버 설정을 바꿀 수 있습니다.")
        context = (public_url, rtsp_host, rtsp_port, Path(data_root))
        if self._context is not None and self._context != context:
            self.discovered_edges = []
            self.discovered_edge_items.clear()
            self.edge_auth_token.clear()
            self.manual_mode.setChecked(False)
            self.edge_camera_id.setText("cam-001")
            self.edge_device_id.setText("edge-001")
            self.edge_mac_address.clear()
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

    def choose_edge_auth_token_file(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self, "Edge 운영 인증 토큰 파일", "", "토큰 파일 (*.token *.txt);;모든 파일 (*)"
        )
        if not filename:
            return
        try:
            with Path(filename).open("rb") as handle:
                raw = handle.read(8193)
            if len(raw) > 8192:
                raise ValueError
            text = raw.decode("utf-8")
            token = text.rstrip("\r\n")
            if (
                len(token) < 32
                or token != token.strip()
                or text not in {token, token + "\n", token + "\r\n"}
                or any(ord(char) < 0x21 or ord(char) > 0x7E for char in token)
            ):
                raise ValueError
        except (OSError, UnicodeError, ValueError):
            self.status.setText("32자 이상의 ASCII 토큰이 든 유효한 UTF-8 파일을 선택하세요.")
            return
        self.edge_auth_token.setText(token)

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
        task.failed.connect(self._task_failed)
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
            self.edge_auth_token.clear()
        self.busy_changed.emit(False)
        if task is not None:
            task.deleteLater()

    def _task_failed(self, message: str) -> None:
        if self._action == "register":
            self._retry_context = None
        self.status.setText(message)

    def confirm_registration_status(self, cameras: list[dict[str, Any]]) -> None:
        uncertain = self._uncertain_camera_id
        if not uncertain:
            return
        registered = any(item.get("camera_id") == uncertain for item in cameras)
        self._uncertain_camera_id = None
        if registered:
            self.status.setText(
                f"{uncertain} 카메라가 이미 등록되어 있습니다. 재등록하지 말고 등록된 카메라에서 상태 또는 게시 계정을 확인하세요."
            )
        else:
            self.status.setText(
                f"목록에서 {uncertain} 카메라를 찾지 못했습니다. 이제 신규 연결을 다시 시도할 수 있습니다."
            )

    # 작업 결과를 GUI 스레드에서 반영하고 검색 결과가 있으면 첫 장치의 설정을 채운다.
    def _completed(self, result: Any) -> None:
        if self._action == "discover":
            self.discovered_edges = result
            self.discovered_edge_items.clear()
            for edge in result:
                self.discovered_edge_items.addItem(f"{edge.device_id} · {edge.address}")
            if not result:
                self.status.setText(
                    "장치를 찾지 못했습니다. Pi의 discovery 모드, 같은 LAN과 "
                    "방화벽의 UDP 37020 허용 여부를 확인하세요."
                )
                return
            self.discovered_edge_items.setCurrentIndex(0)
            self.select_discovered_edge(0)
            self.status.setText(
                f"Edge {len(result)}대를 찾았습니다. 장치를 선택해 연결하세요."
            )
        elif isinstance(result, dict):
            stage = result.get("stage")
            if stage == "save_failed":
                self._pending_response = result["response"]
                self.save_pending_button.setEnabled(True)
                self.advanced_toggle.setChecked(True)
                self.status.setText(
                    "중앙 등록은 완료됐지만 게시 계정 파일 저장에 실패했습니다. "
                    "재등록하지 말고 파일 저장을 재시도하세요."
                )
            elif stage == "pair_failed":
                self._retry_context = result["context"]
                self.retry_button.show()
                self.status.setText(
                    "중앙 등록과 게시 계정 저장은 완료됐지만 Edge 설정 전달을 확인하지 못했습니다. "
                    "Edge 상태를 확인한 뒤 같은 등록으로 재시도하거나 저장된 파일로 Pi를 설정하세요.\n"
                    f"전달 파일: {result['handoff']}\n오류: {result['message']}"
                )
            elif stage == "paired":
                self._retry_context = None
                self.retry_button.hide()
                self.status.setText(
                    "Edge 설정 전달이 확인됐습니다. 등록된 카메라에서 관리 연결 상태를 확인하세요."
                )
                self.registration_finished.emit()
            elif stage == "manual_saved":
                self._retry_context = None
                self.status.setText(
                    "중앙 등록과 게시 계정 저장이 완료됐습니다. 전달 파일을 Pi에 옮겨 설정하세요.\n"
                    f"전달 파일: {result['handoff']}\n이미 등록된 카메라를 다시 등록하지 마세요."
                )
                self.registration_finished.emit()
            elif stage == "retry_alive":
                self._retry_context = None
                self.retry_button.hide()
                self.status.setText(
                    "Edge가 운영 상태로 응답했습니다. 설정을 반복 전송하지 않았습니다. "
                    "등록된 카메라에서 관리 연결을 새로 조회하세요."
                )
                self.registration_finished.emit()
            elif stage == "registration_uncertain":
                self._retry_context = None
                self._uncertain_camera_id = result["camera_id"]
                self.status.setText(
                    f"{result['camera_id']} 등록 요청의 응답이 끊겨 중앙 등록 여부를 확인할 수 없습니다. "
                    "재등록하지 말고 등록된 카메라 탭에서 목록 새로고침을 누르세요."
                )
            else:
                self.status.setText("작업 결과를 확인하지 못했습니다.")
        else:
            self.status.setText(result)

    # 인증 토큰 없이 백그라운드에서 제한 시간 동안 LAN 광고를 수집한다.
    def discover_edge_devices(self) -> None:
        if self.busy:
            return
        def discover(progress):
            progress("같은 LAN에서 discovery 광고를 보내는 Edge를 찾는 중입니다…")
            try:
                return discover_edges_via_helper(timeout=3.0)
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
        self.edge_mac_address.setText(edge.mac_address)
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
                result = probe_edge_via_helper(edge)
            except Exception as exc:
                raise RuntimeError(_connection_error(exc)) from exc
            if result.get("device_id") != edge.device_id or result.get("camera_id") != edge.camera_id:
                raise RuntimeError("Edge 응답의 장치 ID가 discovery 광고와 다릅니다.")
            if result.get("status") != "pairing":
                raise RuntimeError(
                    "신규 연결은 최초 설정 대기 중인 Edge만 지원합니다. "
                    "이미 설정된 Edge는 기존 Edge 이전 절차를 사용하세요."
                )
            return "Edge 응답과 장치 정보가 일치합니다. 이제 선택한 Edge를 연결할 수 있습니다."

        self._start("probe", probe)

    # UI 입력을 검증·복사한 뒤 중앙 등록과 Edge 설정 전달을 수행하고 수동 인계가 필요하면 계정을 저장한다.
    def register_edge(self) -> None:
        if self.busy:
            return
        if self._uncertain_camera_id:
            self.status.setText(
                "이전 등록 결과가 불확실합니다. 등록된 카메라에서 목록 새로고침으로 먼저 확인하세요."
            )
            return
        edge = self.selected_discovered_edge()
        if edge is None and not self.manual_mode.isChecked():
            self._invalid(
                self.discovered_edge_items,
                "Edge를 찾아 선택하세요. 주소를 수정했다면 고급 설정에서 직접 등록을 선택하세요.",
            )
            return
        for widget, message in (
            (self.edge_name, "카메라 이름을 입력하세요."),
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
            or any(ord(char) < 0x21 or ord(char) > 0x7E for char in token)
        ):
            self._invalid(
                self.edge_auth_token, "Edge 운영 인증 토큰(32자 이상)을 입력하세요."
            )
            return
        # Snapshot every widget before starting work; worker code uses only values.
        camera_id = self.edge_camera_id.text().strip()
        mac_address = self.edge_mac_address.text().strip().lower()
        if not re.fullmatch(r"[0-9a-f]{2}(:[0-9a-f]{2}){5}", mac_address):
            self._invalid(self.edge_mac_address, "MAC 주소를 aa:bb:cc:dd:ee:ff 형식으로 입력하세요.")
            return
        registration = {
            "camera_id": camera_id,
            "name": self.edge_name.text().strip(),
            "edge_device_id": self.edge_device_id.text().strip(),
            "edge_mac_address": mac_address,
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

        context = {
            "edge": edge, "operational_token": token,
            "central_host": central_host, "central_port": central_port,
            "video_profile": profile, "backup_root": backup_root,
            "handoff": output,
        }

        def register(progress):
            try:
                progress("게시 계정 전달 파일 위치를 준비하는 중입니다…")
                handoff = prepare_private_output(output)
                if edge is not None:
                    progress("Edge 응답과 장치 ID를 확인하는 중입니다…")
                    probe = probe_edge_via_helper(edge)
                    if (
                        probe.get("device_id") != edge.device_id
                        or probe.get("camera_id") != edge.camera_id
                    ):
                        raise RuntimeError("Edge 응답의 장치 ID가 discovery 광고와 다릅니다.")
                    if probe.get("status") != "pairing":
                        raise RuntimeError(
                            "신규 연결은 최초 설정 대기 중인 Edge만 지원합니다. "
                            "이미 설정된 Edge는 기존 Edge 이전 절차를 사용하세요."
                        )
                progress("중앙 서버에 Edge와 카메라를 등록하는 중입니다…")
                try:
                    response = self.api.register_edge(**registration)
                except Exception as exc:
                    if getattr(exc, "code", None) == "SERVER_UNREACHABLE":
                        return {
                            "stage": "registration_uncertain",
                            "camera_id": camera_id,
                        }
                    raise
            except Exception as exc:
                raise RuntimeError(_connection_error(exc)) from exc
            context["handoff"] = handoff
            try:
                write_publish_credentials(response, camera_id, handoff)
            except Exception:
                return {"stage": "save_failed", "response": response}
            if edge is None:
                return {"stage": "manual_saved", "handoff": handoff}
            progress("Edge에 영상 연결 설정을 전달하는 중입니다…")
            try:
                pair_edge_via_helper(
                    edge, server_response=response, operational_token=token,
                    central_host=central_host, central_port=central_port,
                    video_profile=profile, backup_root=backup_root,
                )
            except Exception as exc:
                context["response"] = response
                return {
                    "stage": "pair_failed", "context": context,
                    "handoff": handoff, "message": _connection_error(exc),
                }
            return {"stage": "paired"}

        self._retry_context = context if edge is not None else None
        self._start("register", register)

    def save_pending(self) -> None:
        if not self._pending_response:
            return
        try:
            write_publish_credentials(
                self._pending_response,
                self._pending_response["camera_id"],
                Path(self.publish_credentials_output.text()),
            )
        except Exception:
            self.status.setText(
                "중앙 등록은 완료됐습니다. 새로 등록하지 말고 파일 저장을 다시 시도하세요."
            )
            return
        response = self._pending_response
        self._pending_response = None
        self.save_pending_button.setEnabled(False)
        context = self._retry_context
        if context and context.get("edge") is not None:
            context["response"] = response
            self.retry_pairing()
        else:
            self.status.setText("게시 계정을 저장했습니다. 파일을 해당 Pi에 옮겨 설정하세요.")

    def retry_pairing(self) -> None:
        context = self._retry_context
        if not context or self.busy:
            return
        response = context.get("response")
        if response is None:
            self.status.setText("저장된 등록 응답이 없어 전달을 재시도할 수 없습니다.")
            return
        edge = context["edge"]

        def retry(progress):
            progress("Edge의 현재 설정 상태를 확인하는 중입니다…")
            try:
                current = probe_edge_via_helper(edge)
            except Exception as exc:
                return {
                    "stage": "pair_failed", "context": context,
                    "handoff": context["handoff"], "message": _connection_error(exc),
                }
            if current.get("device_id") != edge.device_id or current.get("camera_id") != edge.camera_id:
                return {
                    "stage": "pair_failed", "context": context,
                    "handoff": context["handoff"],
                    "message": "현재 Edge 응답의 장치 ID가 기존 광고와 다릅니다.",
                }
            if current.get("status") == "alive":
                return {"stage": "retry_alive"}
            progress("최초 설정 대기 중인 Edge에 기존 등록 설정을 전달하는 중입니다…")
            try:
                pair_edge_via_helper(
                    edge, server_response=response,
                    operational_token=context["operational_token"],
                    central_host=context["central_host"],
                    central_port=context["central_port"],
                    video_profile=context["video_profile"],
                    backup_root=context["backup_root"],
                )
            except Exception as exc:
                return {
                    "stage": "pair_failed", "context": context,
                    "handoff": context["handoff"], "message": _connection_error(exc),
                }
            return {"stage": "paired"}

        self._start("retry", retry)

    # 요청 중인 QThread가 화면보다 오래 남지 않도록 연결 작업 종료 전에는 닫기를 보류한다.
    def closeEvent(self, event) -> None:
        if self.busy or self.has_pending:
            self.status.setText("연결 작업과 게시 계정 파일 저장을 마친 뒤 이 화면을 닫을 수 있습니다.")
            event.ignore()
        else:
            super().closeEvent(event)
