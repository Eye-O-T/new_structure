"""Retain the reference settings sidebar/stack/save flow; configure server cameras."""

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QFormLayout, QLabel, QLineEdit,
    QPushButton, QComboBox, QScrollArea, QCheckBox, QSizePolicy,
)

from .api import DesktopApi
from .legacy_settings import SettingsWindow as LegacySettingsWindow
from .tasks import TaskOwner
from .local_camera import (
    LocalCameraPublisher,
    list_camera_devices,
    publisher_status,
)
from server.operations.local_camera import (
    load_device_setting,
    load_local_camera_enabled,
    save_device_setting,
    save_local_camera_enabled,
)


def _breakable_text(value: str) -> str:
    text = str(value or "")
    for sep in ("/", "\\", "_", "-"):
        text = text.replace(sep, sep + "\u200b")
    return text


class SettingsWindow(TaskOwner, LegacySettingsWindow):
    def __init__(self, parent=None, **kwargs):
        self.api = parent.api
        self.local_publisher = getattr(parent, "local_publisher", None)
        if self.local_publisher is None:
            data_root = getattr(parent, "storage_root_path", "") or None
            self.local_publisher = LocalCameraPublisher(parent, data_root=data_root)
            parent.local_publisher = self.local_publisher
        super().__init__(parent, **kwargs)
        self.body = self.pages
        if self.api:
            self.message.setText("로그인 상태가 유지되고 있습니다.")
        self.bounding_boxes_enabled = bool(getattr(parent, "_overlay_enabled", True))

    def create_basic_page(self):
        page = QWidget()
        page.setMinimumWidth(0)
        page.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout = QVBoxLayout(page)
        layout.setContentsMargins(30, 24, 30, 24)
        layout.setSpacing(12)

        def section(title):
            label = QLabel(title)
            label.setWordWrap(True)
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
        self.local_camera_device = QComboBox()
        self.local_camera_enabled = QCheckBox("전체 녹화 시 노트북 카메라 송출")
        for widget in (self.username, self.password, self.camera):
            style_input(widget)
        style_input(self.local_camera_device)
        self._local_devices = list_camera_devices()
        self.local_camera_device.addItems(self._local_devices)
        saved_device = load_device_setting(self.local_publisher.data_root)
        if saved_device:
            saved_index = self.local_camera_device.findText(saved_device)
            if saved_index >= 0:
                self.local_camera_device.setCurrentIndex(saved_index)
        self.local_camera_enabled.setChecked(
            load_local_camera_enabled(self.local_publisher.data_root)
        )
        self.local_camera_device.setEnabled(bool(self._local_devices))
        self._populate_camera_combo()
        if isinstance(self.selected_source, tuple):
            camera_id = self.selected_source[1]
            source_data = (
                ("local", saved_device)
                if camera_id == "local-camera" and saved_device
                else ("server", camera_id)
            )
            self.camera.setCurrentIndex(
                self.camera.findData(source_data)
            )
        server_url = self.parent().server_url
        server_url_label = QLabel(_breakable_text(server_url))
        server_url_label.setWordWrap(True)
        server_url_label.setToolTip(str(server_url))
        form.addRow("서버 주소", server_url_label)
        form.addRow("관리자 계정", self.username)
        form.addRow("비밀번호", self.password)
        form.addRow("카메라", self.camera)
        form.addRow("로컬 카메라 사용", self.local_camera_enabled)
        form.addRow("노트북 카메라 장치", self.local_camera_device)
        layout.addLayout(form)
        self.bounding_box_checkbox = QCheckBox("Bounding Box 표시")
        self.bounding_box_checkbox.setChecked(
            bool(getattr(self.parent(), "_overlay_enabled", True))
        )
        layout.addWidget(self.bounding_box_checkbox)
        trust_label = (
            f"추가 CA: {_breakable_text(self.parent().ca_file)}"
            if self.parent().ca_file
            else "추가 CA 없음 · 운영체제의 신뢰 인증서 사용"
        )
        trust_widget = QLabel(trust_label)
        trust_widget.setWordWrap(True)
        if self.parent().ca_file:
            trust_widget.setToolTip(str(self.parent().ca_file))
        layout.addWidget(trust_widget)
        self.login_button = QPushButton("로그인")
        style_button(self.login_button, "#1d4ed8", "#3b82f6", "#2563eb")
        self.login_button.setEnabled(not bool(self.api))
        self.login_button.clicked.connect(self.login)
        layout.addWidget(self.login_button)
        self.logout_button = QPushButton("로그아웃")
        style_button(self.logout_button, "#334155", "#64748b", "#475569")
        self.logout_button.setEnabled(bool(self.api))
        self.logout_button.clicked.connect(self.logout)
        layout.addWidget(self.logout_button)
        self.refresh_cameras_button = QPushButton("카메라 목록 새로고침")
        style_button(self.refresh_cameras_button, "#334155", "#64748b", "#475569")
        self.refresh_cameras_button.setEnabled(bool(self.api))
        self.refresh_cameras_button.clicked.connect(self.refresh_cameras)
        layout.addWidget(self.refresh_cameras_button)
        layout.addSpacing(16)
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
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(page)
        return scroll

    def _populate_camera_combo(self):
        current = self.camera.currentData() if self.camera.count() else None
        self.camera.clear()
        for item in self.parent().cameras:
            camera_id = item["camera_id"]
            if camera_id == "local-camera" and self._local_devices:
                continue
            self.camera.addItem(
                f"{item.get('name', '')} · {camera_id}",
                ("server", camera_id),
            )
        for device in self._local_devices:
            self.camera.addItem(f"노트북 웹캠 · {device}", ("local", device))
        if current is not None:
            index = self.camera.findData(current)
            if index >= 0:
                self.camera.setCurrentIndex(index)

    def login(self):
        owner = self.parent()
        url, username, password = owner.server_url, self.username.text().strip(), self.password.text()
        ca_file, previous = owner.ca_file, owner.api
        allow_insecure_http = url.lower().startswith("http://") and getattr(owner, "allow_insecure_http", False)
        session_store = getattr(owner, "session_store", None)
        self.password.clear()

        def operation():
            api = DesktopApi(url, ca_file=ca_file, allow_insecure_http=allow_insecure_http, session_store=session_store)
            try:
                api.login(username, password)
            except Exception:
                try:
                    api.logout(clear_saved=False)
                except Exception:
                    pass
                raise
            if previous:
                try:
                    previous.logout(clear_saved=False)
                except Exception:
                    pass
            return api

        def completed(api):
            self.api = api
            owner.api, owner.cameras = self.api, []
            owner.username = username
            self._populate_camera_combo()
            self.refresh_cameras_button.setEnabled(True)
            self.login_button.setEnabled(False)
            self.logout_button.setEnabled(True)
            if api.session_persistence_error:
                self.message.setText("로그인했지만 로그인 상태를 저장하지 못했습니다.")
                return
            self.message.setText("로그인했습니다. 카메라 목록 새로고침을 눌러 목록을 불러오세요.")

        self.execute(operation, completed)

    def logout(self):
        if not self.api:
            return
        api = self.api

        def completed(_result):
            owner = self.parent()
            stop_display = getattr(owner, "stop_display_workers", None)
            if stop_display is not None:
                stop_display()
            self.api = owner.api = None
            self.selected_source = owner.video_source = 0
            owner.cameras = []
            if hasattr(owner, "_camera_recordings"):
                owner._camera_recordings.clear()
            owner._selected_camera_id = None
            owner._refresh_camera_picker()
            owner._show_views()
            owner.cam_status.setText("설정에서 로그인하세요.")
            self._populate_camera_combo()
            self.refresh_cameras_button.setEnabled(False)
            self.login_button.setEnabled(True)
            self.logout_button.setEnabled(False)
            self.message.setText("로그아웃했습니다.")

        self.execute(api.logout, completed)

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
        self._populate_camera_combo()
        updated = getattr(self.parent(), "cameras_updated", None)
        if updated is not None:
            updated(cameras)
        self.message.setText("카메라 목록을 갱신했습니다.")

    def save_basic_settings(self):
        if self.task is not None:
            return
        local_enabled = self.local_camera_enabled.isChecked()
        local_device = self.local_camera_device.currentText().strip()
        selection = self.camera.currentData()
        if selection and selection[0] == "local":
            local_device = str(selection[1]).strip()
            self.local_camera_device.setCurrentText(local_device)
        if local_enabled and not local_device:
            self.message.setText("사용할 노트북 카메라 장치를 선택하세요.")
            return
        current_publisher = publisher_status(self.local_publisher.data_root)
        previous_device = load_device_setting(self.local_publisher.data_root)
        save_local_camera_enabled(self.local_publisher.data_root, local_enabled)
        if local_device:
            save_device_setting(self.local_publisher.data_root, local_device)
        owner = self.parent()
        publisher_device_changed = (
            current_publisher.get("status") == "running"
            and current_publisher.get("device_name") != local_device
        )
        selected_device_changed = (
            previous_device is not None
            and previous_device != local_device
            and bool(getattr(owner, "_local_camera_preview_requested", False))
        )
        should_pause_preview = (
            not local_enabled
            or publisher_device_changed
            or selected_device_changed
        )
        should_stop_publisher = current_publisher.get("status") == "running" and (
            not local_enabled or publisher_device_changed
        )
        if should_pause_preview:
            pause_preview = getattr(owner, "pause_local_camera_preview", None)
            if pause_preview is not None:
                pause_preview(
                    "노트북 카메라 설정 변경에 따라 기존 송출을 중지하는 중입니다."
                )
            if should_stop_publisher:
                try:
                    self.local_publisher.stop()
                except OSError as exc:
                    set_stopped_message = getattr(
                        owner, "_set_local_camera_stopped_message", None
                    )
                    if set_stopped_message is not None:
                        set_stopped_message(f"로컬 카메라 중지 확인 실패: {exc}")
                    self.message.setText(
                        f"노트북 카메라를 중지하지 못했습니다. 설정은 저장됐습니다: {exc}"
                    )
                    return
            set_stopped_message = getattr(
                owner, "_set_local_camera_stopped_message", None
            )
            if set_stopped_message is not None:
                set_stopped_message("노트북 카메라 설정 변경으로 미리보기가 중지되었습니다.")
        if not self.api or not selection:
            self.message.setText("로그인 후 카메라를 선택하세요.")
            return
        kind, value = selection
        self.bounding_boxes_enabled = self.bounding_box_checkbox.isChecked()
        if kind == "local":
            self.selected_source = (self.api, "local-camera")
            self.accept()
            return
        self.selected_source = (self.api, value)
        self.accept()

    def create_storage_page(self):
        # The reference local-folder and clip writers must not duplicate server recording.
        raw_path = self.ai_cctv_path or self.storage_root_path or "서버에서 관리"
        return self.create_empty_page(
            "저장 설정",
            f"서버 저장소: {_breakable_text(raw_path)}\n\n"
            "녹화·감지·분석은 서버가 수행합니다.\n"
            "저장 위치와 녹화 정책은 설치 도우미의 운영 설정을 사용합니다.\n"
            "실시간 영상은 관제 창이 열려 있을 때 자동 연결되며, 녹화 시작·중지 설정은 서버에 유지됩니다.",
        )
