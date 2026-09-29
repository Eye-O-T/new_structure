"""Reference main-window flow with server-backed operations and safe Qt lifetimes."""

import argparse
import sys

from PyQt5.QtCore import QProcess, QTimer, Qt
from PyQt5.QtWidgets import QApplication, QLabel, QMessageBox

from .legacy_gui import CCTVMainWindow as ReferenceWindow
from ..qt_tasks import BackgroundTask


class CCTVMainWindow(ReferenceWindow):
    def __init__(self, server_url="https://localhost", username="admin", storage_path="", ca_file=None, allow_insecure_http=False, rtsp_host="127.0.0.1", rtsp_port=8554):
        self.server_url, self.username, self.ca_file = server_url, username, ca_file
        self.allow_insecure_http = allow_insecure_http
        self.rtsp_host, self.rtsp_port = rtsp_host, rtsp_port
        self.api = None
        self.cameras = []
        self._closing = False
        self._logout_task = None
        self._logout_done = False
        self._stopping = False
        super().__init__()
        self.ai_cctv_path = self.storage_root_path = str(storage_path)
        self.setMinimumSize(1100, 700)
        self.video_label.setMinimumSize(480, 270)
        self.storage_label.setText(f"서버 저장소\n{storage_path or '서버에서 관리'}\n\nSTOP·창 닫기는 모니터링만 종료합니다.\n서버 녹화는 계속됩니다.")
        self.cam_status.setText("설정에서 로그인하세요.")
        self.cam_status.setWordWrap(True)
        for label in self.findChildren(QLabel):
            label.setTextFormat(Qt.PlainText)
            if label.text() == "카메라\nRTSP / LAN / USB 입력 상태":
                label.setText("카메라\n서버 연결 상태")
            if label.text() == "누적 추적":
                label.setText("관측 추적 (이번 START)")
            if label.text() == "CAM-01 정문 · 실시간 분석 화면":
                self.camera_title = label
                label.setText("서버 카메라 · 실시간 영상")
        self.btn_stop.setEnabled(False)

    def start_video(self):
        if self.worker is not None:
            return
        if not self.api or not isinstance(self.video_source, tuple) or self.video_source[0] is not self.api:
            self.open_settings()
        if not self.api or not isinstance(self.video_source, tuple) or self.video_source[0] is not self.api:
            return
        self.appear_count = self.disappear_count = 0
        for metric in (self.metric_current, self.metric_total, self.metric_appear, self.metric_disappear):
            metric["value"].setText("0")
        while self.event_list.count():
            widget = self.event_list.takeAt(0).widget()
            if widget:
                widget.deleteLater()
        self._stopping = False
        super().start_video()
        self.btn_start.setEnabled(False)
        self.btn_setting.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.cam_status.setText(f"● {self.video_source[1]} · 로딩 중")

    def stop_video(self):
        if self.worker is not None:
            self._stopping = True
            self.worker.stop()
            self.cam_status.setText("영상 연결을 정리하고 있습니다…")
            self.btn_stop.setEnabled(False)
        else:
            self.show_idle_screen()

    def handle_worker_finished(self):
        worker = self.worker
        if worker is None:
            return
        self.worker = None
        worker.deleteLater()
        self.btn_start.setEnabled(True)
        self.btn_setting.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.cam_status.setText("모니터링 중지됨" if self._stopping else "영상 연결 종료 — START로 재시도")
        self.show_idle_screen()
        if self._closing:
            QTimer.singleShot(0, self.close)

    def update_frame(self, frame):
        if self.worker is None:
            return
        try:
            if not self._stopping:
                super().update_frame(frame)
                self.cam_status.setText(f"● {self.video_source[1]} · LIVE")
        finally:
            self.worker.acknowledge_frame()

    def open_settings(self):
        if self.worker is not None:
            return
        if self.resource_monitor_window is not None:
            self.resource_monitor_window.close()
            if self.resource_monitor_window is not None:
                QMessageBox.information(self, "상태 조회 중", "리소스 조회가 끝난 뒤 설정을 열어 주세요.")
                return
        super().open_settings()
        if isinstance(self.video_source, tuple):
            camera_id = self.video_source[1]
            self.camera_title.setText(f"{camera_id} · 실시간 영상")
            self.cam_status.setText(f"● {camera_id} · 준비됨")
        self.storage_label.setText(f"서버 저장소\n{self.ai_cctv_path or '서버에서 관리'}\n\nSTOP·창 닫기는 모니터링만 종료합니다.\n서버 녹화는 계속됩니다.")

    def open_resource_monitor(self):
        if not self.api:
            self.open_settings()
        if self.api:
            super().open_resource_monitor()

    def _build_resource_monitor_url(self, source):
        return self.server_url

    def add_event(self, event):
        if self._stopping:
            return
        if event.get("type") == "network_failure" and isinstance(self.video_source, tuple):
            camera_id = self.video_source[1]
            publisher = getattr(self, "local_publisher", None)
            if camera_id == "local-camera" and (
                publisher is None or publisher.process.state() == QProcess.NotRunning
            ):
                event = {
                    **event,
                    "message": "노트북 카메라 송출이 실행 중이 아닙니다. 설정에서 송출 시작을 확인하세요.",
                }
        super().add_event(event)
        # Plain text prevents server-provided labels from becoming rich-text links.
        for label in self.event_list.itemAt(0).widget().findChildren(QLabel):
            label.setTextFormat(Qt.PlainText)
        if event.get("type") == "network_failure" and isinstance(self.video_source, tuple):
            self.cam_status.setText(f"● {self.video_source[1]} · 연결 확인 중")

    def closeEvent(self, event):
        local_publisher = getattr(self, "local_publisher", None)
        if local_publisher is not None:
            local_publisher.stop()
        if self.resource_monitor_window is not None:
            self.resource_monitor_window.close()
            if self.resource_monitor_window is not None:
                event.ignore()
                return
        self._closing = True
        if self.worker is not None:
            self.stop_video()
            event.ignore()
            return
        if self._logout_task is not None:
            event.ignore()
            return
        if self.api and not self._logout_done:
            api = self.api

            def operation(_progress):
                try:
                    api.logout()
                except Exception:
                    pass

            self._logout_task = BackgroundTask(operation, self)

            def finished():
                self._logout_task.deleteLater()
                self._logout_task = None
                self._logout_done = True
                self.close()

            self._logout_task.finished.connect(finished)
            self._logout_task.start()
            event.ignore()
            return
        event.accept()


def main(argv=None):
    parser = argparse.ArgumentParser(description="AI CCTV PyQt 관리자")
    parser.add_argument("--server-url", default="https://localhost")
    parser.add_argument("--ca-file")
    args = parser.parse_args(argv)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    window = CCTVMainWindow(args.server_url, ca_file=args.ca_file)
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
