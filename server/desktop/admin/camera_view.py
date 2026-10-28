"""Per-camera live-view widget used by the desktop monitor grid."""

import time

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
)

from .overlay import overlay_items

_RECORDING_STATE_BADGES = {
    "recording": ("녹화 중", "#ef4444", "#450a0a"),
    "waiting": ("녹화 대기", "#facc15", "#422006"),
    "stopped": ("녹화 중지", "#94a3b8", "#1e293b"),
    "error": ("오류", "#ef4444", "#450a0a"),
}


class CameraView(QFrame):
    """Keep frame, overlay, live connection, and recording state isolated for one camera."""

    selected = pyqtSignal(str)
    activated = pyqtSignal(str)
    recording_start_requested = pyqtSignal(str)
    recording_stop_requested = pyqtSignal(str)

    def __init__(self, camera_id, name, parent=None):
        super().__init__(parent)
        self.camera_id = camera_id
        self.name = name or camera_id
        self._frame = None
        self._objects = None
        self._objects_received_at = 0.0
        self._overlay_enabled = True
        self._selected = False
        self.recording_state = "stopped"
        self.recording_enabled = False
        self._recording_busy = False

        self.setFrameShape(QFrame.StyledPanel)
        self.setMinimumSize(220, 150)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setStyleSheet(
            "QFrame { background-color: #0f172a; border: 1px solid #334155; border-radius: 5px; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(4)

        header_layout = QHBoxLayout()
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(6)
        self.title = QLabel(f"{self.name} · {camera_id}")
        self.title.setTextFormat(Qt.PlainText)
        self.title.setWordWrap(True)
        self.title.setStyleSheet("font-size: 13px; font-weight: bold; border: none;")

        self.recording_status = QLabel("녹화 중지")
        self.recording_status.setTextFormat(Qt.PlainText)
        self.recording_status.setAlignment(Qt.AlignCenter)
        self.recording_status.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        header_layout.addWidget(self.title, stretch=1)
        header_layout.addWidget(self.recording_status)

        self.surface = QLabel("LIVE VIDEO SURFACE")
        self.surface.setAlignment(Qt.AlignCenter)
        self.surface.setWordWrap(True)
        self.surface.setMinimumSize(1, 1)
        self.surface.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.surface.setStyleSheet(
            "color: #475569; font-size: 15px; font-weight: bold; border: none;"
        )

        footer_layout = QHBoxLayout()
        footer_layout.setContentsMargins(0, 0, 0, 0)
        footer_layout.setSpacing(6)
        self.status = QLabel("준비됨")
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        self.status.setStyleSheet("color: #facc15; font-size: 12px; border: none;")

        self.btn_record_start = QPushButton("녹화 시작")
        self.btn_record_start.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self.btn_record_start.setStyleSheet(
            "QPushButton { background-color: #166534; color: #ffffff; "
            "border-radius: 4px; padding: 3px 8px; font-size: 12px; font-weight: bold; }"
            "QPushButton:disabled { background-color: #1e293b; color: #64748b; }"
        )
        self.btn_record_start.clicked.connect(
            lambda _checked=False: self.recording_start_requested.emit(self.camera_id)
        )

        self.btn_record_stop = QPushButton("녹화 중지")
        self.btn_record_stop.setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Fixed)
        self.btn_record_stop.setStyleSheet(
            "QPushButton { background-color: #7f1d1d; color: #ffffff; "
            "border-radius: 4px; padding: 3px 8px; font-size: 12px; font-weight: bold; }"
            "QPushButton:disabled { background-color: #1e293b; color: #64748b; }"
        )
        self.btn_record_stop.clicked.connect(
            lambda _checked=False: self.recording_stop_requested.emit(self.camera_id)
        )

        footer_layout.addWidget(self.status, stretch=1)
        footer_layout.addWidget(self.btn_record_start)
        footer_layout.addWidget(self.btn_record_stop)

        layout.addLayout(header_layout)
        layout.addWidget(self.surface, stretch=1)
        layout.addLayout(footer_layout)
        self.set_recording_state("stopped", recording_enabled=False)

    def mousePressEvent(self, event):
        self.selected.emit(self.camera_id)
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        self.activated.emit(self.camera_id)
        super().mouseDoubleClickEvent(event)

    def resizeEvent(self, event):
        self._render()
        super().resizeEvent(event)

    def set_selected(self, selected):
        self._selected = bool(selected)
        color = "#60a5fa" if self._selected else "#334155"
        self.setStyleSheet(
            f"QFrame {{ background-color: #0f172a; border: 2px solid {color}; border-radius: 5px; }}"
        )

    def set_overlay_enabled(self, enabled):
        self._overlay_enabled = bool(enabled)
        self._render()

    def set_recording_state(
        self,
        state,
        *,
        recording_enabled=None,
        busy=False,
        error_code=None,
        error_message=None,
    ):
        normalized = state if state in _RECORDING_STATE_BADGES else "error"
        self.recording_state = normalized
        if recording_enabled is not None:
            self.recording_enabled = bool(recording_enabled)
        elif normalized in {"recording", "waiting"}:
            self.recording_enabled = True
        elif normalized == "stopped":
            self.recording_enabled = False
        self._recording_busy = bool(busy)

        label_text, fg_color, bg_color = _RECORDING_STATE_BADGES[normalized]
        self.recording_status.setText(label_text)
        details = [str(value) for value in (error_code, error_message) if value]
        self.recording_status.setToolTip(" · ".join(details) or label_text)
        self.recording_status.setStyleSheet(
            f"color: {fg_color}; background-color: {bg_color}; "
            f"border: 1px solid {fg_color}; border-radius: 4px; "
            "padding: 2px 6px; font-size: 11px; font-weight: bold;"
        )

        if self._recording_busy:
            self.btn_record_start.setEnabled(False)
            self.btn_record_stop.setEnabled(False)
        elif normalized == "error":
            self.btn_record_start.setEnabled(True)
            self.btn_record_stop.setEnabled(True)
        elif normalized in {"recording", "waiting"} or self.recording_enabled:
            self.btn_record_start.setEnabled(False)
            self.btn_record_stop.setEnabled(True)
        else:
            self.btn_record_start.setEnabled(True)
            self.btn_record_stop.setEnabled(False)

    def set_status(self, text, color="#facc15"):
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color}; font-size: 12px; border: none;")

    def show_loading(self, message):
        self.set_status(message, "#facc15")
        if self._frame is None:
            self.surface.setPixmap(QPixmap())
            self.surface.setText(message)

    def show_error(self, message):
        self.set_status("연결 오류", "#ef4444")
        self.surface.setPixmap(QPixmap())
        self.surface.setText(message)
        self.surface.setStyleSheet(
            "color: #ef4444; font-size: 13px; font-weight: bold; border: none;"
        )

    def set_frame(self, frame):
        self._frame = QImage(frame)
        self.surface.setStyleSheet(
            "color: #475569; font-size: 15px; font-weight: bold; border: none;"
        )
        self.set_status("LIVE", "#22c55e")
        self._render()

    def set_objects(self, payload):
        self._objects = (
            payload if isinstance(payload, dict) and not payload.get("stale") else None
        )
        self._objects_received_at = time.monotonic() if self._objects else 0.0
        self._render()

    def _render(self):
        if (
            self._frame is None
            or self.surface.width() <= 0
            or self.surface.height() <= 0
        ):
            return
        source = QPixmap.fromImage(self._frame)
        width, height = self.surface.width(), self.surface.height()
        scale = min(width / source.width(), height / source.height())
        display_width, display_height = (
            round(source.width() * scale),
            round(source.height() * scale),
        )
        x, y = (width - display_width) // 2, (height - display_height) // 2
        canvas = QPixmap(width, height)
        canvas.fill(QColor("#0f172a"))
        painter = QPainter(canvas)
        painter.drawPixmap(
            x,
            y,
            source.scaled(
                display_width,
                display_height,
                Qt.KeepAspectRatio,
                Qt.SmoothTransformation,
            ),
        )
        if self._overlay_enabled and time.monotonic() - self._objects_received_at <= 3:
            for entry in overlay_items(self._objects, width, height):
                x1, y1, x2, y2 = entry["rect"]
                item = entry["item"]
                painter.setPen(QPen(QColor("#22c55e"), 2))
                painter.drawRect(round(x1), round(y1), round(x2 - x1), round(y2 - y1))
                identity = (
                    item.get("global_person_id") or item.get("person_id") or "person"
                )
                painter.setFont(QFont("Arial", 9, QFont.Bold))
                painter.setPen(QColor("#ffffff"))
                painter.drawText(round(x1) + 4, max(14, round(y1) - 4), str(identity))
        painter.end()
        self.surface.setText("")
        self.surface.setPixmap(canvas)
