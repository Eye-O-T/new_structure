"""Per-camera live-view widget used by the desktop monitor grid."""

import time

from PyQt5.QtCore import Qt, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QImage, QPainter, QPen, QPixmap
from PyQt5.QtWidgets import QFrame, QLabel, QVBoxLayout

from .overlay import overlay_items


class CameraView(QFrame):
    """Keep frame, overlay and connection state isolated for one camera."""

    selected = pyqtSignal(str)
    activated = pyqtSignal(str)

    def __init__(self, camera_id, name, parent=None):
        super().__init__(parent)
        self.camera_id = camera_id
        self.name = name or camera_id
        self._frame = None
        self._objects = None
        self._objects_received_at = 0.0
        self._overlay_enabled = True
        self._selected = False

        self.setFrameShape(QFrame.StyledPanel)
        self.setMinimumSize(240, 135)
        self.setStyleSheet("QFrame { background-color: #0f172a; border: 1px solid #334155; border-radius: 5px; }")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(5, 5, 5, 5)
        layout.setSpacing(4)
        self.title = QLabel(f"{self.name} · {camera_id}")
        self.title.setTextFormat(Qt.PlainText)
        self.title.setStyleSheet("font-size: 14px; font-weight: bold;")
        self.status = QLabel("준비됨")
        self.status.setTextFormat(Qt.PlainText)
        self.status.setStyleSheet("color: #facc15; font-size: 12px;")
        self.surface = QLabel("LIVE VIDEO SURFACE")
        self.surface.setAlignment(Qt.AlignCenter)
        self.surface.setStyleSheet("color: #475569; font-size: 16px; font-weight: bold;")
        layout.addWidget(self.title)
        layout.addWidget(self.surface, stretch=1)
        layout.addWidget(self.status)

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
        self.setStyleSheet(f"QFrame {{ background-color: #0f172a; border: 2px solid {color}; border-radius: 5px; }}")

    def set_overlay_enabled(self, enabled):
        self._overlay_enabled = bool(enabled)
        self._render()

    def set_status(self, text, color="#facc15"):
        self.status.setText(text)
        self.status.setStyleSheet(f"color: {color}; font-size: 12px;")

    def show_loading(self, message):
        self.set_status(message, "#facc15")
        if self._frame is None:
            self.surface.setPixmap(QPixmap())
            self.surface.setText(message)

    def show_error(self, message):
        self.set_status("연결 오류", "#ef4444")
        self.surface.setPixmap(QPixmap())
        self.surface.setText(message)
        self.surface.setStyleSheet("color: #ef4444; font-size: 14px; font-weight: bold;")

    def set_frame(self, frame):
        self._frame = QImage(frame)
        self.surface.setStyleSheet("color: #475569; font-size: 16px; font-weight: bold;")
        self.set_status("LIVE", "#22c55e")
        self._render()

    def set_objects(self, payload):
        self._objects = payload if isinstance(payload, dict) and not payload.get("stale") else None
        self._objects_received_at = time.monotonic() if self._objects else 0.0
        self._render()

    def _render(self):
        if self._frame is None or self.surface.width() <= 0 or self.surface.height() <= 0:
            return
        source = QPixmap.fromImage(self._frame)
        width, height = self.surface.width(), self.surface.height()
        scale = min(width / source.width(), height / source.height())
        display_width, display_height = round(source.width() * scale), round(source.height() * scale)
        x, y = (width - display_width) // 2, (height - display_height) // 2
        canvas = QPixmap(width, height)
        canvas.fill(QColor("#0f172a"))
        painter = QPainter(canvas)
        painter.drawPixmap(x, y, source.scaled(display_width, display_height, Qt.KeepAspectRatio, Qt.SmoothTransformation))
        if self._overlay_enabled and time.monotonic() - self._objects_received_at <= 3:
            for entry in overlay_items(self._objects, width, height):
                x1, y1, x2, y2 = entry["rect"]
                item = entry["item"]
                painter.setPen(QPen(QColor("#22c55e"), 2))
                painter.drawRect(round(x1), round(y1), round(x2 - x1), round(y2 - y1))
                identity = item.get("global_person_id") or item.get("person_id") or "person"
                painter.setFont(QFont("Arial", 9, QFont.Bold))
                painter.setPen(QColor("#ffffff"))
                painter.drawText(round(x1) + 4, max(14, round(y1) - 4), str(identity))
        painter.end()
        self.surface.setText("")
        self.surface.setPixmap(canvas)
