# Display widgets preserved from Eye-O-T/AI_CCTV at 09db3ed464772cd139e45168b91ad46e0f3f9901.
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor, QPainter, QPen
from PyQt5.QtWidgets import QWidget, QFrame, QVBoxLayout, QHBoxLayout, QLabel, QProgressBar

class SparklineChart(QWidget):
    def __init__(self, color="#38bdf8", parent=None):
        super().__init__(parent)
        self.values = []
        self.max_points = 60
        self.color = QColor(color)
        self.setMinimumHeight(78)

    def add_value(self, value):
        self.values.append(max(0.0, min(100.0, float(value))))
        if len(self.values) > self.max_points:
            self.values = self.values[-self.max_points:]
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        rect = self.rect().adjusted(8, 8, -8, -8)

        painter.fillRect(self.rect(), QColor("#0f172a"))
        painter.setPen(QPen(QColor("#334155"), 1))
        painter.drawRect(rect)

        if len(self.values) < 2:
            return

        step = rect.width() / max(1, self.max_points - 1)
        start_index = self.max_points - len(self.values)
        points = []
        for i, value in enumerate(self.values):
            x = rect.left() + (start_index + i) * step
            y = rect.bottom() - (value / 100.0) * rect.height()
            points.append((x, y))

        painter.setPen(QPen(self.color, 2))
        for i in range(1, len(points)):
            painter.drawLine(
                int(points[i - 1][0]),
                int(points[i - 1][1]),
                int(points[i][0]),
                int(points[i][1]),
            )


class ResourceCard(QFrame):
    def __init__(self, title, accent="#38bdf8", parent=None):
        super().__init__(parent)
        self.setStyleSheet(
            "QFrame { background-color: #1e293b; border-radius: 8px; }"
            "QLabel { background: transparent; }"
            "QProgressBar { background-color: #0f172a; border: none; "
            "border-radius: 4px; height: 10px; text-align: center; }"
            "QProgressBar::chunk { background-color: "
            + accent
            + "; border-radius: 4px; }"
        )

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        header_layout = QHBoxLayout()
        self.title_label = QLabel(title)
        self.title_label.setStyleSheet("font-size: 15px; font-weight: bold;")
        self.value_label = QLabel("-")
        self.value_label.setAlignment(Qt.AlignRight)
        self.value_label.setStyleSheet(
            f"font-size: 18px; font-weight: bold; color: {accent};"
        )
        header_layout.addWidget(self.title_label)
        header_layout.addWidget(self.value_label)
        layout.addLayout(header_layout)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        layout.addWidget(self.progress)

        self.detail_label = QLabel("-")
        self.detail_label.setWordWrap(True)
        self.detail_label.setStyleSheet("color: #94a3b8; font-size: 12px;")
        layout.addWidget(self.detail_label)

        self.chart = SparklineChart(accent)
        layout.addWidget(self.chart)

    def update_data(self, percent, value_text, detail_text):
        safe_percent = 0 if percent is None else max(0, min(100, int(percent)))
        self.progress.setValue(safe_percent)
        self.value_label.setText(value_text)
        self.detail_label.setText(detail_text)
        self.chart.add_value(safe_percent)
