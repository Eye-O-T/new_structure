"""Retain the reference settings sidebar/stack/save flow; configure server cameras."""

from time import sleep
from urllib.error import HTTPError

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QFormLayout, QLabel, QLineEdit,
    QPushButton, QComboBox, QMessageBox, QScrollArea,
)

from .api import DesktopApi
from ..server_api import ServerApiError
from .legacy_settings import SettingsWindow as LegacySettingsWindow
from .tasks import TaskOwner
from .local_camera import LocalCameraPublisher, list_camera_devices, rtsp_publish_url


class SettingsWindow(TaskOwner, LegacySettingsWindow):
    def __init__(self, parent=None, **kwargs):
        self.api = parent.api
        self.local_publisher = getattr(parent, "local_publisher", None)
        if self.local_publisher is None:
            self.local_publisher = LocalCameraPublisher(parent)
            parent.local_publisher = self.local_publisher
        self.local_publisher.state_changed.connect(self._local_camera_state)
        super().__init__(parent, **kwargs)
        self.body = self.pages

    def create_basic_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(30, 24, 30, 24)
        layout.setSpacing(12)

        def section(title):
            label = QLabel(title)
            label.setStyleSheet("color: #f8fafc; font-size: 18px; font-weight: bold;")
            layout.addWidget(label)

        def style_button(button, background, border, hover):
            button.setMinimumHeight(40)
            button.setStyleSheet(
                "QPushButton {"
                f"background-color: {background}; border: 1px solid {border};"
                "border-radius: 7px; color: #ffffff; font-size: 14px;"
                "font-weight: bold; padding: 8px 16px;"
                "}"
                f"QPushButton:hover {{ background-color: {hover}; }}"
                "QPushButton:disabled { background-color: #334155;"
                "border-color: #475569; color: #94a3b8; }"
            )

        def style_input(widget):
            widget.setMinimumHeight(34)
            widget.setStyleSheet(
                "QLineEdit, QComboBox { background-color: #0b1220; color: #f8fafc; "
                "border: 2px solid #64748b; border-radius: 5px; "
                "padding: 5px 8px; }"
                "QLineEdit:focus, QComboBox:focus { border-color: #60a5fa; }"
                "QLineEdit:disabled, QComboBox:disabled { "
                "background-color: #1e293b; color: #94a3b8; }"
            )

        section("서버 로그인 · 모니터링 카메라 선택")
        form = QFormLayout()
        self.username = QLineEdit(self.parent().username)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.camera = QComboBox()
        for widget in (self.username, self.password, self.camera):
            style_input(widget)
        for item in self.parent().cameras:
            self.camera.addItem(f"{item.get('name', '')} · {item['camera_id']}", item["camera_id"])
        if isinstance(self.selected_source, tuple):
            self.camera.setCurrentIndex(self.camera.findData(self.selected_source[1]))
        server_url = self.parent().server_url
        form.addRow("서버 주소", QLabel(server_url))
        form.addRow("관리자 계정", self.username)
        form.addRow("비밀번호", self.password)
        form.addRow("카메라", self.camera)
        layout.addLayout(form)
        trust_label = (
            f"추가 CA: {self.parent().ca_file}"
            if self.parent().ca_file
            else "추가 CA 없음 · 운영체제의 신뢰 인증서 사용"
        )
        layout.addWidget(QLabel(trust_label))
        login = QPushButton("로그인")
        style_button(login, "#1d4ed8", "#3b82f6", "#2563eb")
        login.clicked.connect(self.login)
        layout.addWidget(login)
        self.refresh_cameras_button = QPushButton("카메라 목록 새로고침")
        style_button(self.refresh_cameras_button, "#334155", "#64748b", "#475569")
        self.refresh_cameras_button.setEnabled(bool(self.api))
        self.refresh_cameras_button.clicked.connect(self.refresh_cameras)
        layout.addWidget(self.refresh_cameras_button)
        layout.addSpacing(16)
        section("노트북 웹캠")
        local_hint = QLabel(
            "이 PC의 웹캠을 서버 카메라로 등록할 때만 사용합니다. "
            "송출 시작은 FFmpeg를 실행하고, 송출 중지는 이 PC의 FFmpeg를 종료합니다."
        )
        local_hint.setWordWrap(True)
        layout.addWidget(local_hint)
        local = QFormLayout()
        self.local_camera_name = QComboBox()
        devices = list_camera_devices()
        self._has_local_devices = bool(devices)
        self.local_camera_name.addItems(devices)
        if not devices:
            self.local_camera_name.addItem("카메라 장치를 찾지 못했습니다")
        self.local_camera_id = QLineEdit("local-camera")
        owner = self.parent()
        self.local_rtsp_host = QLineEdit(getattr(owner, "rtsp_host", "127.0.0.1"))
        self.local_rtsp_port = QLineEdit(str(getattr(owner, "rtsp_port", 8554)))
        for widget in (
            self.local_camera_name,
            self.local_camera_id,
            self.local_rtsp_host,
            self.local_rtsp_port,
        ):
            style_input(widget)
        local.addRow("노트북 카메라 장치명", self.local_camera_name)
        local.addRow("로컬 카메라 ID", self.local_camera_id)
        local.addRow("RTSP 서버 주소", self.local_rtsp_host)
        local.addRow("RTSP 포트", self.local_rtsp_port)
        layout.addLayout(local)
        self.local_start = QPushButton("노트북 카메라 송출 시작")
        self.local_stop = QPushButton("노트북 카메라 송출 중지")
        self.local_start.setEnabled(bool(devices))
        self.local_stop.setEnabled(False)
        style_button(self.local_start, "#15803d", "#22c55e", "#16a34a")
        style_button(self.local_stop, "#b91c1c", "#ef4444", "#dc2626")
        self.local_start.clicked.connect(self.start_local_camera)
        self.local_stop.clicked.connect(self.stop_local_camera)
        layout.addWidget(self.local_start)
        layout.addWidget(self.local_stop)
        manage = QPushButton("카메라 등록 · 운영 설정")
        style_button(manage, "#334155", "#64748b", "#475569")
        manage.clicked.connect(self.manage)
        layout.addWidget(manage)
        layout.addSpacing(16)
        section("설정 완료")
        self.btn_save = QPushButton("저장")
        style_button(self.btn_save, "#1d4ed8", "#3b82f6", "#2563eb")
        self.btn_save.clicked.connect(self.save_basic_settings)
        layout.addWidget(self.btn_save)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(page)
        return scroll

    def _local_camera_state(self, state):
        messages = {
            "starting": "노트북 카메라 송출을 시작하는 중입니다.",
            "failed": "노트북 카메라 송출이 실패했습니다. FFmpeg·카메라·RTSP 주소를 확인하세요.",
            "stopped": "노트북 카메라 송출이 중지되었습니다.",
        }
        if state == "failed":
            self.message.setText(self.local_publisher.failure_message or messages[state])
        elif state in messages and hasattr(self, "message"):
            self.message.setText(messages[state])
        if hasattr(self, "local_start"):
            running = state in {"starting", "running"}
            self.local_start.setEnabled(self._has_local_devices and not running)
            self.local_stop.setEnabled(running)

    def _local_camera_task_failed(self, _message):
        self.local_start.setEnabled(self._has_local_devices)
        self.local_stop.setEnabled(False)

    def start_local_camera(self):
        if not self.api:
            self.message.setText("먼저 서버에 로그인하세요.")
            return
        camera_id = self.local_camera_id.text().strip()
        name = self.local_camera_name.currentText().strip()
        if not camera_id or not name:
            self.message.setText("카메라 ID와 장치명을 입력하세요.")
            return
        host, port = self.local_rtsp_host.text().strip(), self.local_rtsp_port.text().strip()
        try:
            if not 1 <= int(port) <= 65535:
                raise ValueError
        except ValueError:
            self.message.setText("RTSP 포트는 1~65535 범위여야 합니다.")
            return
        if any(item.get("camera_id") == camera_id for item in self.parent().cameras):
            answer = QMessageBox.question(
                self,
                "기존 카메라 송출 설정 변경",
                "같은 카메라 ID가 이미 등록되어 있습니다.\n"
                "게시 계정이 재발급되어 기존 송출이 중단될 수 있습니다.\n\n"
                "계속하시겠습니까?",
            )
            if answer != QMessageBox.Yes:
                return

        def register():
            try:
                return self.api.register_local_camera(camera_id, name)
            except ServerApiError as exc:
                if exc.status_code != 409:
                    raise
                self.api.camera(camera_id, method="PATCH", payload={"name": name, "enabled": True})
                return self.api.camera(camera_id, "/publish-credentials/rotate", "POST")

        def started(result):
            credentials = result.get("publish_credentials") or {}
            try:
                url = rtsp_publish_url(host, port, camera_id, credentials["username"], credentials["password"])
                self.local_publisher.start(name, url)
                current = [item for item in self.parent().cameras if item.get("camera_id") != camera_id]
                current.append({"camera_id": camera_id, "name": result.get("name", name)})
                self.parent().cameras = current
                self.camera.clear()
                for item in self.parent().cameras:
                    self.camera.addItem(f"{item.get('name', '')} · {item['camera_id']}", item["camera_id"])
                self.camera.setCurrentIndex(self.camera.findData(camera_id))
                self.message.setText("RTSP 서버에서 웹캠 영상을 확인하는 중입니다…")
                QTimer.singleShot(0, lambda: self._verify_local_camera(camera_id))
            except Exception as exc:
                self.message.setText(str(exc))

        self.local_start.setEnabled(False)
        self.local_stop.setEnabled(False)
        self.execute(register, started)
        if self.task is not None:
            self.task.failed.connect(self._local_camera_task_failed)

    def _verify_local_camera(self, camera_id):
        if self.task is not None:
            QTimer.singleShot(100, lambda: self._verify_local_camera(camera_id))
            return

        def operation():
            try:
                live = self.api.camera(camera_id, "/live")
                playlist_path = self.api.media_path(camera_id, live["hls_url"])
            except Exception:
                return False, "카메라 영상 주소를 확인할 수 없습니다. 로그인과 카메라 등록 상태를 확인하세요."

            for _ in range(15):
                if self.local_publisher.failure_message:
                    return False, self.local_publisher.failure_message
                try:
                    playlist = self.api.media(camera_id, playlist_path).decode("utf-8")
                    if any(line.strip() and not line.startswith("#") for line in playlist.splitlines()):
                        return True, "노트북 카메라 송출이 확인됐습니다. 메인 화면에서 모니터링을 시작하세요."
                except HTTPError as exc:
                    if exc.code in {401, 403}:
                        return False, "영상 인증에 실패했습니다. 카메라를 다시 등록한 뒤 송출을 시작하세요."
                    if exc.code not in {404, 502, 503}:
                        return False, "서버가 웹캠 영상을 거부했습니다. RTSP 송출 계정과 서버 설정을 확인하세요."
                except Exception:
                    pass
                sleep(1)

            return False, "15초 안에 서버에서 웹캠 영상을 받지 못했습니다. 송출 오류와 RTSP 주소·포트 8554를 확인하세요."

        def completed(result):
            ready, message = result
            if not ready:
                self.local_publisher.stop()
            self.message.setText(message)

        self.execute(operation, completed)

    def stop_local_camera(self):
        self.local_publisher.stop()
        self.local_start.setEnabled(self._has_local_devices)
        self.local_stop.setEnabled(False)
        self.message.setText("노트북 카메라 송출을 중지했습니다.")

    def login(self):
        owner = self.parent()
        url, username, password = owner.server_url, self.username.text().strip(), self.password.text()
        ca_file, previous = owner.ca_file, owner.api
        self.password.clear()

        def operation():
            api = DesktopApi(url, ca_file=ca_file, allow_insecure_http=url.lower().startswith("http://") and getattr(self.parent(), "allow_insecure_http", False))
            try:
                api.login(username, password)
            except Exception:
                try:
                    api.logout()
                except Exception:
                    pass
                raise
            if previous:
                try:
                    previous.logout()
                except Exception:
                    pass
            return api

        def completed(api):
            self.api = api
            owner.api, owner.cameras = self.api, []
            owner.username = username
            self.camera.clear()
            self.refresh_cameras_button.setEnabled(True)
            self.message.setText("로그인했습니다. 카메라 목록 새로고침을 눌러 목록을 불러오세요.")

        self.execute(operation, completed)

    def refresh_cameras(self):
        if not self.api:
            self.message.setText("먼저 로그인하세요.")
            return
        self.execute(self.api.cameras, self.reload_cameras)

    def manage(self):
        if not self.api:
            self.message.setText("먼저 로그인하세요.")
            return
        from .management import CameraManagement

        dialog = CameraManagement(self.api, self)
        dialog.exec_()
        self.execute(self.api.cameras, self.reload_cameras)

    def reload_cameras(self, cameras):
        self.parent().cameras = cameras
        self.camera.clear()
        for item in cameras:
            self.camera.addItem(f"{item.get('name', '')} · {item['camera_id']}", item["camera_id"])
        self.message.setText("카메라 목록을 갱신했습니다.")

    def save_basic_settings(self):
        if self.task is not None:
            return
        if not self.api or not self.camera.currentData():
            self.message.setText("로그인 후 카메라를 선택하세요.")
            return
        self.selected_source = (self.api, self.camera.currentData())
        self.accept()

    def create_storage_page(self):
        # The reference local-folder and clip writers must not duplicate server recording.
        return self.create_empty_page(
            "저장 설정",
            f"서버 저장소: {self.ai_cctv_path or self.storage_root_path or '서버에서 관리'}\n\n"
            "녹화·감지·분석은 서버가 수행합니다.\n"
            "저장 위치와 녹화 정책은 설치 도우미의 운영 설정을 사용합니다.\n"
            "모니터링 시작·중지는 이 창의 화면 연결만 제어합니다.",
        )
