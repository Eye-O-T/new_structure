import cv2
from datetime import datetime
from urllib.parse import urlparse

from PyQt5.QtWidgets import (
    QMainWindow,
    QWidget,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QFrame,
    QScrollArea,
)
from PyQt5.QtCore import Qt
from PyQt5.QtGui import QImage, QPixmap
from ui.settings_window import SettingsWindow
from ui.resource_monitor_window import ResourceMonitorWindow
from workers.video_worker import VideoWorker


class CCTVMainWindow(QMainWindow):
    def __init__(self):
        super().__init__()

        self.setWindowTitle("Intelligent CCTV Control Center")
        self.setGeometry(100, 100, 1600, 900)
        self.setStyleSheet(
            "background-color: #0f172a; color: #f8fafc; font-family: Arial;"
        )

        self.worker = None
        self.appear_count = 0
        self.disappear_count = 0
        self.video_source = 0
        self.use_yolo = True
        self.use_vlm = True
        self.storage_root_path = ""
        self.ai_cctv_path = ""
        self.original_segment_seconds = 10
        self.clip_max_seconds = 10
        self.resource_monitor_window = None

        self.init_ui()

    def init_ui(self):
        central_widget = QWidget()
        self.setCentralWidget(central_widget)

        main_layout = QVBoxLayout(central_widget)
        main_layout.setContentsMargins(20, 20, 20, 20)
        main_layout.setSpacing(20)

        header_layout = QHBoxLayout()

        title_label = QLabel("Intelligent CCTV Control Center")
        title_label.setStyleSheet("font-size: 28px; font-weight: bold;")

        self.btn_start = QPushButton("START")
        self.btn_start.setStyleSheet(
            "background-color: #166534; color: white; padding: 8px 20px; "
            "border-radius: 5px; font-size: 22px; font-weight: bold;"
        )
        self.btn_start.clicked.connect(self.start_video)

        self.btn_stop = QPushButton("STOP")
        self.btn_stop.setStyleSheet(
            "background-color: #7f1d1d; color: white; padding: 8px 20px; "
            "border-radius: 5px; font-size: 22px; font-weight: bold;"
        )
        self.btn_stop.clicked.connect(self.stop_video)
        self.btn_setting = QPushButton("설정")
        self.btn_setting.setStyleSheet(
            "background-color: #334155; color: white; padding: 8px 20px; "
            "border-radius: 5px; font-size: 22px; font-weight: bold;"
        )
        self.btn_setting.clicked.connect(self.open_settings)

        self.btn_resource_monitor = QPushButton("리소스 모니터링")
        self.btn_resource_monitor.setStyleSheet(
            "background-color: #0e7490; color: white; padding: 8px 20px; "
            "border-radius: 5px; font-size: 22px; font-weight: bold;"
        )
        self.btn_resource_monitor.clicked.connect(self.open_resource_monitor)

        header_layout.addWidget(title_label)
        header_layout.addStretch()
        header_layout.addWidget(self.btn_start)
        header_layout.addWidget(self.btn_stop)
        header_layout.addWidget(self.btn_setting)
        header_layout.addWidget(self.btn_resource_monitor)

        main_layout.addLayout(header_layout)

        body_layout = QHBoxLayout()
        body_layout.setSpacing(20)
        main_layout.addLayout(body_layout)

        left_panel = QFrame()
        left_panel.setFixedWidth(300)
        left_panel.setStyleSheet("background-color: #1e293b; border-radius: 10px;")

        left_layout = QVBoxLayout(left_panel)

        cam_label = QLabel("카메라\nRTSP / LAN / USB 입력 상태")
        cam_label.setStyleSheet("color: #94a3b8; font-size: 17px;")
        left_layout.addWidget(cam_label)

        self.cam_status = QLabel("● CAM-01 · 대기 중")
        self.cam_status.setStyleSheet(
            "background-color: #0f172a; border: 1px solid #3b82f6; "
            "border-radius: 5px; padding: 15px; color: #facc15; font-size: 22px;"
        )
        left_layout.addWidget(self.cam_status)
        left_layout.addStretch()

        body_layout.addWidget(left_panel)

        center_panel = QFrame()
        center_panel.setStyleSheet("background-color: #1e293b; border-radius: 10px;")
        center_layout = QVBoxLayout(center_panel)

        center_title = QLabel("CAM-01 정문 · 실시간 분석 화면")
        center_title.setStyleSheet("font-size: 22px; font-weight: bold;")
        center_layout.addWidget(center_title)

        self.video_label = QLabel("LIVE VIDEO SURFACE")
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setStyleSheet(
            "background-color: #0f172a; border-radius: 5px; "
            "font-size: 28px; color: #334155; font-weight: bold;"
        )
        self.video_label.setMinimumSize(800, 450)
        center_layout.addWidget(self.video_label, stretch=1)

        metrics_layout = QHBoxLayout()

        self.metric_current = self.create_metric_box("0", "현재 객체")
        self.metric_total = self.create_metric_box("0", "누적 추적")
        self.metric_appear = self.create_metric_box("0", "출현")
        self.metric_disappear = self.create_metric_box("0", "사라짐")

        metrics_layout.addWidget(self.metric_current["box"])
        metrics_layout.addWidget(self.metric_total["box"])
        metrics_layout.addWidget(self.metric_appear["box"])
        metrics_layout.addWidget(self.metric_disappear["box"])

        center_layout.addLayout(metrics_layout)

        body_layout.addWidget(center_panel, stretch=1)

        right_panel = QFrame()
        right_panel.setFixedWidth(350)
        right_panel.setStyleSheet("background-color: #1e293b; border-radius: 10px;")

        right_layout = QVBoxLayout(right_panel)

        event_label = QLabel("이벤트 타임라인\n출현 · 이동 · 사라짐 중심")
        event_label.setStyleSheet("color: #94a3b8; font-size: 17px;")
        right_layout.addWidget(event_label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("border: none;")

        scroll_widget = QWidget()
        self.event_list = QVBoxLayout(scroll_widget)
        self.event_list.setAlignment(Qt.AlignTop)

        scroll.setWidget(scroll_widget)

        right_layout.addWidget(scroll)
        right_layout.addStretch()

        self.storage_label = QLabel(
            "저장 경로\n"
            "저장 경로가 설정되지 않았습니다.\n\n"
            "설정 - 저장 설정에서 위치를 선택하세요."
        )
        self.storage_label.setWordWrap(True)
        self.storage_label.setStyleSheet("font-size: 17px; font-weight: bold;")
        right_layout.addWidget(self.storage_label)

        body_layout.addWidget(right_panel)

    def create_metric_box(self, value, label):
        box = QFrame()
        box.setStyleSheet("background-color: #0f172a; border-radius: 5px;")

        layout = QVBoxLayout(box)

        value_label = QLabel(value)
        value_label.setStyleSheet("font-size: 34px; font-weight: bold;")

        text_label = QLabel(label)
        text_label.setStyleSheet("color: #cbd5e1; font-size: 22px;")

        layout.addWidget(value_label)
        layout.addWidget(text_label)

        return {
            "box": box,
            "value": value_label,
            "label": text_label
        }

    def start_video(self):
        if self.worker is not None:
            return

        source = self.video_source

        self.worker = VideoWorker(
            source=source,
            use_yolo=self.use_yolo,
            use_vlm=self.use_vlm,
            ai_cctv_path=self.ai_cctv_path,
            original_segment_seconds=self.original_segment_seconds,
            clip_max_seconds=self.clip_max_seconds
        )
        self.worker.frame_ready.connect(self.update_frame)
        self.worker.metrics_ready.connect(self.update_metrics)
        self.worker.event_ready.connect(self.add_event)
        self.worker.loading_ready.connect(self.show_loading_screen)
        self.worker.finished.connect(self.handle_worker_finished)
        self.show_loading_screen("시스템 시작 중...")
        self.worker.start()

        self.cam_status.setText("● CAM-01 · 로딩 중")
        self.cam_status.setStyleSheet(
            "background-color: #0f172a; border: 1px solid #facc15; "
            "border-radius: 5px; padding: 15px; color: #facc15; font-size: 22px;"
        )

    def stop_video(self):
        if self.worker is not None:
            self.worker.stop()
            self.worker = None

        self.cam_status.setText("● CAM-01 · 중지됨")
        self.cam_status.setStyleSheet(
            "background-color: #0f172a; border: 1px solid #ef4444; "
            "border-radius: 5px; padding: 15px; color: #ef4444; font-size: 22px;"
        )
        self.show_idle_screen()

    def open_settings(self):
        dialog = SettingsWindow(
            self,
            video_source=self.video_source,
            use_yolo=self.use_yolo,
            use_vlm=self.use_vlm,
            storage_root_path=self.storage_root_path,
            ai_cctv_path=self.ai_cctv_path,
            original_segment_seconds=self.original_segment_seconds,
            clip_max_seconds=self.clip_max_seconds
        )

        if dialog.exec_():
            self.video_source = dialog.selected_source
            self.use_yolo = dialog.use_yolo
            self.use_vlm = dialog.use_vlm

            self.storage_root_path = dialog.storage_root_path
            self.ai_cctv_path = dialog.ai_cctv_path
            self.original_segment_seconds = dialog.original_segment_seconds
            self.clip_max_seconds = dialog.clip_max_seconds

            self.cam_status.setText(
                f"● CAM-01 · 입력 설정 완료: {self.video_source}"
            )

            if self.ai_cctv_path:
                self.storage_label.setText(
                    "저장 경로\n"
                    f"{self.ai_cctv_path}\n\n"
                    "하위 폴더\n"
                    "원본 녹화본\n"
                    "이벤트 CLIP(YOLO 사용 시)"
                )
            else:
                self.storage_label.setText(
                    "저장 경로\n"
                    "저장 경로가 설정되지 않았습니다.\n\n"
                    "설정 → 저장 설정에서 위치를 선택하세요."
                )

    def _build_resource_monitor_url(self, source):
        parsed = urlparse(str(source))
        host = parsed.hostname
        if not host:
            return None
        return f"http://{host}:8002"

    def open_resource_monitor(self):
        if self.resource_monitor_window is None:
            self.resource_monitor_window = ResourceMonitorWindow(
                self,
                storage_path=self.ai_cctv_path or self.storage_root_path,
                resource_server_url=self._build_resource_monitor_url(
                    self.video_source
                )
            )
            self.resource_monitor_window.finished.connect(
                self.handle_resource_monitor_closed
            )

        self.resource_monitor_window.show()
        self.resource_monitor_window.raise_()
        self.resource_monitor_window.activateWindow()

    def handle_resource_monitor_closed(self):
        self.resource_monitor_window = None

    def update_frame(self, frame):
        if self.cam_status.text() != "● CAM-01 · LIVE":
            self.cam_status.setText("● CAM-01 · LIVE")
            self.cam_status.setStyleSheet(
                "background-color: #0f172a; border: 1px solid #22c55e; "
                "border-radius: 5px; padding: 15px; color: #22c55e; font-size: 22px;"
            )

        self.video_label.setStyleSheet(
            "background-color: #0f172a; border-radius: 5px; "
            "font-size: 28px; color: #334155; font-weight: bold;"
        )

        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)

        h, w, ch = rgb_frame.shape
        bytes_per_line = ch * w

        qt_img = QImage(
            rgb_frame.data,
            w,
            h,
            bytes_per_line,
            QImage.Format_RGB888
        )

        pixmap = QPixmap.fromImage(qt_img)
        scaled_pixmap = pixmap.scaled(
            self.video_label.width(),
            self.video_label.height(),
            Qt.KeepAspectRatio
        )

        self.video_label.setPixmap(scaled_pixmap)

    def set_camera_status(self, text, border_color, text_color):
        self.cam_status.setText(text)
        self.cam_status.setStyleSheet(
            f"background-color: #0f172a; border: 1px solid {border_color}; "
            f"border-radius: 5px; padding: 15px; color: {text_color}; font-size: 22px;"
        )

    def show_loading_screen(self, message):
        self.video_label.clear()
        self.video_label.setText(f"{message}\n잠시만 기다려 주세요.")
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setStyleSheet(
            "background-color: #0f172a; border: 1px solid #334155; "
            "border-radius: 5px; font-size: 28px; color: #facc15; "
            "font-weight: bold;"
        )

    def show_idle_screen(self):
        self.video_label.clear()
        self.video_label.setText("LIVE VIDEO SURFACE")
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setStyleSheet(
            "background-color: #0f172a; border-radius: 5px; "
            "font-size: 28px; color: #334155; font-weight: bold;"
        )

    def show_network_failure_screen(self):
        self.video_label.clear()
        self.video_label.setText(
            "네트워크 연결 장애\n네트워크 연결 상태를 확인하세요."
        )
        self.video_label.setAlignment(Qt.AlignCenter)
        self.video_label.setStyleSheet(
            "background-color: #000000; border: 1px solid #ef4444; "
            "border-radius: 5px; font-size: 28px; color: #ef4444; "
            "font-weight: bold;"
        )

    def handle_worker_finished(self):
        if self.worker is None:
            return

        if not self.worker.running:
            return

        self.worker = None
        self.cam_status.setText("● CAM-01 · 오류")
        self.cam_status.setStyleSheet(
            "background-color: #0f172a; border: 1px solid #ef4444; "
            "border-radius: 5px; padding: 15px; color: #ef4444; font-size: 22px;"
        )

    def update_metrics(self, data):
        self.metric_current["value"].setText(str(data.get("current_objects", 0)))
        self.metric_total["value"].setText(str(data.get("tracked_total", 0)))

    def add_event(self, event):
        event_type = event.get("type", "unknown")
        person_id = event.get("person_id", "-")
        time_text = event.get("time", datetime.now().strftime("%H:%M:%S"))

        if event_type == "appear":
            self.appear_count += 1
            self.metric_appear["value"].setText(str(self.appear_count))
            desc = f"ID {person_id} 출현"
            color = "#22c55e"
        elif event_type == "disappear":
            self.disappear_count += 1
            self.metric_disappear["value"].setText(str(self.disappear_count))
            desc = f"ID {person_id} 사라짐"
            color = "#f97316"
        elif event_type == "error":
            desc = event.get("message", "오류 발생")
            color = "#ef4444"
        elif event_type == "network_failure":
            desc = event.get("message", "네트워크 장애 감지")
            color = "#facc15"
            self.set_camera_status(
                "● CAM-01 · 네트워크 장애",
                "#facc15",
                "#facc15"
            )
            self.show_network_failure_screen()
        elif event_type == "network_recovered":
            desc = event.get("message", "장애 복구 영상 저장 완료")
            color = "#38bdf8"
            self.set_camera_status(
                "● CAM-01 · LIVE",
                "#22c55e",
                "#22c55e"
            )
        else:
            desc = f"ID {person_id} {event_type}"
            color = "#38bdf8"

        event_box = QFrame()
        event_box.setStyleSheet("background-color: #0f172a; border-radius: 5px;")

        layout = QVBoxLayout(event_box)
        layout.setContentsMargins(18, 14, 18, 14)
        layout.setSpacing(8)

        time_label = QLabel(time_text)
        time_label.setStyleSheet(f"color: {color}; font-size: 24px; font-weight: bold;")

        desc_label = QLabel(desc)
        desc_label.setWordWrap(True)
        desc_label.setStyleSheet("font-size: 18px; font-weight: bold;")

        layout.addWidget(time_label)
        layout.addWidget(desc_label)

        self.event_list.insertWidget(0, event_box)
        if self.event_list.count() > 30:
            old_item = self.event_list.takeAt(30)

            if old_item:
                widget = old_item.widget()

                if widget:
                    widget.deleteLater()

    def closeEvent(self, event):
        self.stop_video()
        event.accept()

