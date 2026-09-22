"""Native equivalents of the web camera management actions."""

import json
from pathlib import Path

from PyQt5.QtWidgets import (
    QDialog, QWidget, QVBoxLayout, QFormLayout, QComboBox, QLineEdit,
    QPushButton, QLabel, QPlainTextEdit, QFileDialog, QMessageBox, QScrollArea,
)

from ..server_api import prepare_private_output, redact_for_display, write_publish_credentials
from .tasks import TaskOwner


class CameraManagement(TaskOwner, QDialog):
    def __init__(self, api, parent=None):
        super().__init__(parent)
        self.api = api
        self.pending = None
        self.output_path = None
        self.setWindowTitle("카메라 등록 · 운영 설정")
        self.resize(760, 800)
        layout = QVBoxLayout(self)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        self.body = QWidget()
        scroll.setWidget(self.body)
        layout.addWidget(scroll)
        body = QVBoxLayout(self.body)
        self.camera = QComboBox()
        body.addWidget(self.camera)
        refresh = QPushButton("목록 새로고침")
        refresh.clicked.connect(self.refresh)
        body.addWidget(refresh)
        status = QPushButton("선택한 카메라 상태 · 지원 화질 조회")
        status.clicked.connect(self.inspect)
        body.addWidget(status)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        body.addWidget(self.details)
        self.profile = QComboBox()
        body.addWidget(self.profile)
        self.apply_profile = QPushButton("화질 적용")
        self.apply_profile.setEnabled(False)
        self.apply_profile.clicked.connect(self.set_profile)
        body.addWidget(self.apply_profile)
        self.camera.currentIndexChanged.connect(self.reset_profile)
        body.addWidget(QLabel("새 등록에는 모든 항목 입력 / 수정에는 변경할 항목만 입력"))
        form = QFormLayout()
        self.fields = {}
        for key, label in [("camera_id", "새 카메라 ID"), ("name", "이름"),
                           ("edge_device_id", "Edge 장치 ID"), ("edge_management_url", "Edge 관리 주소"),
                           ("edge_recovery_url", "Edge 복구 주소"), ("edge_auth_token", "새 Edge 인증 토큰")]:
            entry = QLineEdit()
            if key == "edge_auth_token":
                entry.setEchoMode(QLineEdit.Password)
            self.fields[key] = entry
            form.addRow(label, entry)
        self.enabled = QComboBox()
        for text, value in [("변경하지 않음", None), ("사용", True), ("중지", False)]:
            self.enabled.addItem(text, value)
        form.addRow("사용 설정", self.enabled)
        body.addLayout(form)
        for text, callback in [("새 카메라 등록", self.register), ("선택한 카메라 수정", self.update),
                               ("게시 계정 재발급", self.rotate)]:
            button = QPushButton(text)
            button.clicked.connect(callback)
            body.addWidget(button)
        self.save_button = QPushButton("미저장 게시 계정 파일 저장 재시도")
        self.save_button.clicked.connect(self.save_pending)
        self.save_button.setEnabled(False)
        body.addWidget(self.save_button)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.refresh()

    def reset_profile(self):
        self.profile.clear()
        self.apply_profile.setEnabled(False)
        self.details.clear()

    def refresh(self):
        def completed(items):
            self.camera.clear()
            for item in items:
                self.camera.addItem(f"{item.get('name', '')} · {item['camera_id']}", item["camera_id"])
            self.message.setText("목록을 갱신했습니다.")
        self.execute(self.api.cameras, completed)

    def inspect(self):
        camera_id = self.camera.currentData()
        if not camera_id:
            return
        self.reset_profile()

        def operation():
            results = {}
            for key, suffix in [("status", "/status"), ("profile", "/video-profile")]:
                try:
                    results[key] = self.api.camera(camera_id, suffix)
                except Exception:
                    results[key] = {"error": "조회 실패"}
            return results

        def completed(result):
            self.details.setPlainText(json.dumps(redact_for_display(result), ensure_ascii=False, indent=2))
            profile = result["profile"]
            if result["status"].get("error") or profile.get("error"):
                self.apply_profile.setEnabled(False)
                self.message.setText("카메라 상태 또는 화질 정보를 조회하지 못했습니다.")
                return
            for value in profile.get("supported_profiles", []):
                if value in {"hd", "fhd"}:
                    self.profile.addItem(value.upper(), value)
            self.profile.setCurrentIndex(self.profile.findData(profile.get("current_profile")))
            self.apply_profile.setEnabled(profile.get("edge_online") is True and self.profile.count() > 0)
            self.message.setText("상태를 조회했습니다.")
        self.execute(operation, completed)

    def set_profile(self):
        camera_id, profile = self.camera.currentData(), self.profile.currentData()
        if camera_id and profile:
            self.execute(lambda: self.api.camera(camera_id, "/video-profile", "PATCH", {"profile": profile}), self.changed)

    def changed(self, result):
        self.details.setPlainText(json.dumps(redact_for_display(result), ensure_ascii=False, indent=2))
        self.fields["edge_auth_token"].clear()
        self.message.setText("요청을 처리했습니다. 상태를 다시 조회해 확인하세요.")

    def update(self):
        camera_id = self.camera.currentData()
        values = {key: field.text().strip() for key, field in self.fields.items() if key != "camera_id" and field.text().strip()}
        if self.enabled.currentData() is not None:
            values["enabled"] = self.enabled.currentData()
        if camera_id and values:
            self.execute(lambda: self.api.camera(camera_id, method="PATCH", payload=values), self.changed)

    def choose_output(self):
        if self.pending:
            self.message.setText("먼저 미저장 게시 계정을 저장하세요.")
            return False
        path, _ = QFileDialog.getSaveFileName(self, "게시 계정 저장 위치", "publish.json", "JSON (*.json)")
        if not path:
            return False
        try:
            self.output_path = prepare_private_output(Path(path))
        except OSError:
            self.message.setText("파일을 저장할 수 있는 위치를 선택하세요.")
            return False
        return True

    def register(self):
        values = {key: field.text().strip() for key, field in self.fields.items()}
        if not all(values.values()):
            self.message.setText("등록할 카메라의 모든 항목을 입력하세요.")
            return
        if self.choose_output():
            self.execute(lambda: self.api.register_edge(**values), self.handoff)

    def rotate(self):
        camera_id = self.camera.currentData()
        if not camera_id or self.pending:
            return
        if QMessageBox.question(self, "게시 계정 재발급", "기존 영상 송출이 끊깁니다. 새 계정을 Edge에 적용할 준비가 되었습니까?") != QMessageBox.Yes:
            return
        if self.choose_output():
            self.execute(lambda: self.api.camera(camera_id, "/publish-credentials/rotate", "POST"), self.handoff)

    def handoff(self, result):
        self.pending = result
        self.fields["edge_auth_token"].clear()
        self.save_pending()

    def save_pending(self):
        if not self.pending:
            return
        try:
            write_publish_credentials(self.pending, self.pending["camera_id"], self.output_path)
        except Exception:
            self.save_button.setEnabled(True)
            self.message.setText("서버 처리는 완료됐지만 저장에 실패했습니다. 재발급하지 말고 저장을 재시도하세요.")
            path, _ = QFileDialog.getSaveFileName(self, "다른 저장 위치 선택", "publish.json", "JSON (*.json)")
            if path:
                self.output_path = Path(path)
            return
        self.pending = None
        self.save_button.setEnabled(False)
        self.message.setText("게시 계정을 저장했습니다. 이 파일을 해당 Edge에 적용하세요. 목록 새로고침으로 등록 결과를 확인하세요.")

    def reject(self):
        if self.pending:
            self.message.setText("게시 계정을 저장한 뒤 닫으세요.")
            return
        super().reject()

    def closeEvent(self, event):
        if self.pending:
            event.ignore()
            self.message.setText("게시 계정을 저장한 뒤 닫으세요.")
        else:
            super().closeEvent(event)
