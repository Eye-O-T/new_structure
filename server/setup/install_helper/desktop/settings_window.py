"""Retain the reference settings sidebar/stack/save flow; configure server cameras."""

from PyQt5.QtWidgets import (
    QWidget, QVBoxLayout, QFormLayout, QLabel, QLineEdit,
    QPushButton, QComboBox, QFileDialog,
)

from .api import DesktopApi
from ..server_api import ServerApiError
from .legacy_settings import SettingsWindow as LegacySettingsWindow
from .tasks import TaskOwner
from .local_camera import LocalCameraPublisher, list_camera_devices, rtsp_publish_url


class SettingsWindow(TaskOwner, LegacySettingsWindow):
    def __init__(self, parent=None, **kwargs):
        self.api = parent.api
        self.ca_file = parent.ca_file
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
        layout.addWidget(QLabel("서버 로그인 · 모니터링 카메라 선택"))
        form = QFormLayout()
        self.server_url = QLineEdit(self.parent().server_url)
        self.username = QLineEdit(self.parent().username)
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.Password)
        self.camera = QComboBox()
        for item in self.parent().cameras:
            self.camera.addItem(f"{item.get('name', '')} · {item['camera_id']}", item["camera_id"])
        if isinstance(self.selected_source, tuple):
            self.camera.setCurrentIndex(self.camera.findData(self.selected_source[1]))
        form.addRow("HTTPS 서버 주소", self.server_url)
        form.addRow("관리자 계정", self.username)
        form.addRow("비밀번호", self.password)
        form.addRow("카메라", self.camera)
        layout.addLayout(form)
        ca = QPushButton("추가 신뢰 인증서 선택 (선택 사항)")
        ca.clicked.connect(self.select_ca)
        layout.addWidget(ca)
        self.ca_label = QLabel(str(self.ca_file or "운영체제의 신뢰 인증서 사용"))
        layout.addWidget(self.ca_label)
        login = QPushButton("로그인 · 카메라 목록 불러오기")
        login.clicked.connect(self.login)
        layout.addWidget(login)
        local = QFormLayout()
        self.local_camera_name = QComboBox()
        devices = list_camera_devices() or ["Integrated Camera"]
        self.local_camera_name.addItems(devices)
        self.local_camera_id = QLineEdit("local-camera")
        owner = self.parent()
        self.local_rtsp_host = QLineEdit(getattr(owner, "rtsp_host", "127.0.0.1"))
        self.local_rtsp_port = QLineEdit(str(getattr(owner, "rtsp_port", 8554)))
        local.addRow("노트북 카메라 장치명", self.local_camera_name)
        local.addRow("로컬 카메라 ID", self.local_camera_id)
        local.addRow("RTSP 서버 주소", self.local_rtsp_host)
        local.addRow("RTSP 포트", self.local_rtsp_port)
        layout.addLayout(local)
        self.local_start = QPushButton("노트북 카메라 송출 시작")
        self.local_stop = QPushButton("노트북 카메라 송출 중지")
        self.local_start.clicked.connect(self.start_local_camera)
        self.local_stop.clicked.connect(self.stop_local_camera)
        layout.addWidget(self.local_start)
        layout.addWidget(self.local_stop)
        manage = QPushButton("카메라 등록 · 운영 설정")
        manage.clicked.connect(self.manage)
        layout.addWidget(manage)
        self.btn_save = QPushButton("저장")
        self.btn_save.clicked.connect(self.save_basic_settings)
        layout.addWidget(self.btn_save)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        layout.addStretch()
        return page

    def _local_camera_state(self, state):
        messages = {
            "starting": "노트북 카메라 송출을 시작하는 중입니다.",
            "failed": "노트북 카메라 송출이 실패했습니다. FFmpeg·카메라·RTSP 주소를 확인하세요.",
            "stopped": "노트북 카메라 송출이 중지되었습니다.",
        }
        if state in messages and hasattr(self, "message"):
            self.message.setText(messages[state])

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
                self.message.setText("노트북 카메라 송출을 시작했습니다.")
            except Exception as exc:
                self.message.setText(str(exc))

        self.execute(register, started)

    def stop_local_camera(self):
        self.local_publisher.stop()
        self.message.setText("노트북 카메라 송출을 중지했습니다.")

    def select_ca(self):
        path, _ = QFileDialog.getOpenFileName(self, "신뢰할 인증서 선택", "", "인증서 (*.crt *.pem)")
        if path:
            self.ca_file = path
            self.ca_label.setText(path)

    def login(self):
        url, username, password = self.server_url.text().strip(), self.username.text().strip(), self.password.text()
        ca_file, previous = self.ca_file, self.parent().api
        self.password.clear()

        def operation():
            api = DesktopApi(url, ca_file=ca_file, allow_insecure_http=url.lower().startswith("http://") and getattr(self.parent(), "allow_insecure_http", False))
            try:
                api.login(username, password)
                cameras = api.cameras()
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
            return api, cameras

        def completed(result):
            self.api, cameras = result
            owner = self.parent()
            owner.api, owner.cameras = self.api, cameras
            owner.server_url, owner.username, owner.ca_file = url, username, ca_file
            self.camera.clear()
            for item in cameras:
                self.camera.addItem(f"{item.get('name', '')} · {item['camera_id']}", item["camera_id"])
            self.message.setText("로그인했습니다. 카메라를 선택하고 저장하세요." if cameras else "로그인했습니다. 카메라를 등록하세요.")

        self.execute(operation, completed)

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
            "START / STOP은 이 창의 영상 모니터링만 시작·중지합니다.",
        )
