"""Reference resource cards fed by current authenticated diagnostics."""

import json

from PyQt5.QtCore import QTimer
from PyQt5.QtWidgets import QDialog, QWidget, QVBoxLayout, QHBoxLayout, QLabel, QPlainTextEdit

from ..server_api import redact_for_display
from .resource_cards import ResourceCard
from .tasks import TaskOwner


class ResourceMonitorWindow(TaskOwner, QDialog):
    def __init__(self, parent=None, storage_path="", resource_server_url=None):
        super().__init__(parent)
        self.api = parent.api
        self.setWindowTitle("리소스 모니터링")
        self.resize(950, 700)
        layout = QVBoxLayout(self)
        self.body = QWidget()
        layout.addWidget(self.body)
        body = QVBoxLayout(self.body)
        row = QHBoxLayout()
        self.cpu = ResourceCard("선택한 Edge CPU")
        self.memory = ResourceCard("선택한 Edge 메모리")
        self.storage = ResourceCard("서버 녹화 저장소")
        for card in (self.cpu, self.memory, self.storage):
            row.addWidget(card)
        body.addLayout(row)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        body.addWidget(self.details)
        self.message = QLabel("")
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        self.timer = QTimer(self)
        self.timer.setInterval(5000)
        self.timer.timeout.connect(self.refresh)
        self.timer.start()
        self.finished.connect(self.timer.stop)
        self.refresh()

    def refresh(self):
        if not self.api or self.task is not None:
            return
        source = self.parent().video_source
        camera_id = source[1] if isinstance(source, tuple) else None

        def operation():
            status = self.api.system_status()
            edge = self.api.camera(camera_id, "/status") if camera_id else {}
            return status, edge

        def completed(result):
            status, edge = result
            for card, key in [(self.cpu, "cpu_percent"), (self.memory, "memory_percent")]:
                value = edge.get(key)
                card.update_data(value, f"{value}%" if value is not None else "—", edge.get("last_seen_at") or "아직 관측되지 않음")
            volume = status.get("data", {}).get("storage", {}).get("volumes", {}).get("recordings", {})
            free = volume.get("free_percent")
            used = 100 - free if isinstance(free, (float, int)) else None
            self.storage.update_data(used, f"{used:.1f}%" if used is not None else "—", "사용 중 / " + str(volume.get("status", "정보 없음")))
            self.details.setPlainText(json.dumps(redact_for_display(status), ensure_ascii=False, indent=2))
            self.message.setText("서버·감지·저장소 상태를 갱신했습니다.")
        self.execute(operation, completed)
