"""Reference main-window flow with server-backed operations and safe Qt lifetimes."""

import argparse
import logging
import sys
import time

from PyQt5.QtCore import QProcess, QTimer, Qt, QUrl
from PyQt5.QtGui import QDesktopServices, QPixmap
from PyQt5.QtWidgets import QApplication, QDialog, QDialogButtonBox, QLabel, QMessageBox, QPushButton, QVBoxLayout

from .legacy_gui import CCTVMainWindow as ReferenceWindow
from .api import safe_error
from ..qt_tasks import BackgroundTask


LOGGER = logging.getLogger(__name__)
_RECORDING_STATUS_LABELS = {
    "ready": "재생 가능",
    "writing": "처리 중",
    "missing": "파일 없음",
    "corrupt": "손상됨",
    "deleting": "삭제 중",
    "deleted": "삭제됨 / 보관기간 만료",
}


class CCTVMainWindow(ReferenceWindow):
    def __init__(self, server_url="https://localhost", username="admin", storage_path="", ca_file=None, allow_insecure_http=False, rtsp_host="127.0.0.1", rtsp_port=8554):
        self.server_url, self.username, self.ca_file = server_url, username, ca_file
        self.allow_insecure_http = allow_insecure_http
        self.rtsp_host, self.rtsp_port = rtsp_host, rtsp_port
        self.api = None
        self.cameras = []
        self._closing = False
        self._logout_task = None
        self._event_detail_tasks = set()
        self._logout_done = False
        self._stopping = False
        self._render_count = 0
        self._render_total_ms = 0.0
        self._last_render_log = time.monotonic()
        super().__init__()
        self.ai_cctv_path = self.storage_root_path = str(storage_path)
        self.setMinimumSize(900, 600)
        self.video_label.setMinimumSize(320, 180)
        self.storage_label.setText(f"서버 저장소\n{storage_path or '서버에서 관리'}\n\n모니터링 중지는 화면 연결만 종료합니다.\n서버 녹화는 계속됩니다.")
        self.cam_status.setText("설정에서 로그인하세요.")
        self.cam_status.setWordWrap(True)
        for label in self.findChildren(QLabel):
            label.setTextFormat(Qt.PlainText)
            if label.text() == "카메라\nRTSP / LAN / USB 입력 상태":
                label.setText("카메라\n서버 연결 상태")
            if label.text() == "누적 추적":
                label.setText("관측 추적 (이번 시작)")
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
        self.cam_status.setText("모니터링 중지됨" if self._stopping else "영상 연결 종료 — 모니터링 시작으로 재시도")
        self.show_idle_screen()
        if self._closing:
            QTimer.singleShot(0, self.close)

    def update_frame(self, frame):
        if self.worker is None:
            return
        started = time.perf_counter()
        try:
            if not self._stopping:
                super().update_frame(frame)
                self.cam_status.setText(f"● {self.video_source[1]} · LIVE")
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            self._render_count += 1
            self._render_total_ms += elapsed_ms
            now = time.monotonic()
            if now - self._last_render_log >= 5:
                average_ms = self._render_total_ms / max(1, self._render_count)
                LOGGER.info(
                    "video render diagnostics: frames=%d average_ms=%.2f last_ms=%.2f",
                    self._render_count,
                    average_ms,
                    elapsed_ms,
                )
                self._last_render_log = now
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
        self.storage_label.setText(f"서버 저장소\n{self.ai_cctv_path or '서버에서 관리'}\n\n모니터링 중지는 화면 연결만 종료합니다.\n서버 녹화는 계속됩니다.")

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
            if publisher is not None and camera_id == publisher.camera_id and (
                publisher.process.state() == QProcess.NotRunning
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

    def open_event_detail(self, event):
        event_id = event.get("id")
        if not event_id or not self.api:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle(f"이벤트 상세: {event_id}")
        dialog.setMinimumWidth(520)
        layout = QVBoxLayout(dialog)
        loading = QLabel("이벤트 정보를 불러오는 중입니다.")
        layout.addWidget(loading)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        state = {"open": True}
        dialog.destroyed.connect(lambda: state.__setitem__("open", False))

        def operation(_progress):
            detail = self.api.event(event_id)
            media_results = []
            media = detail.get("media") if isinstance(detail.get("media"), dict) else {}
            for kind, key, label in (
                ("snapshot", "snapshot", "스냅샷"),
                ("crop", "crop", "Person Crop"),
                ("annotated-snapshot", "annotated_snapshot", "분석 이미지"),
            ):
                if not media.get(key):
                    continue
                try:
                    content, content_type = self.api.event_image(event_id, kind)
                    media_results.append((label, content, content_type, None))
                except Exception as exc:
                    media_results.append((label, None, None, safe_error(exc)))
            recording_results = []
            for recording_id in detail.get("recording_segment_ids", []):
                try:
                    recording = self.api.recording(recording_id)
                    playback = (
                        self.api.recording_playback(recording_id)
                        if recording.get("status") == "ready" else None
                    )
                    recording_results.append((recording_id, recording, playback, None))
                except Exception as exc:
                    recording_results.append((recording_id, None, None, safe_error(exc)))
            return detail, media_results, recording_results

        def render(result):
            if not state["open"] or not dialog.isVisible():
                return
            loading.deleteLater()
            detail, media_results, recording_results = result
            for name, value in (
                ("Event ID", detail.get("id", event_id)),
                ("이벤트 유형", detail.get("event_type", "-")),
                ("카메라", detail.get("camera_id", "-")),
                ("발생 시각", detail.get("occurred_at", "-")),
                ("Person ID", detail.get("person_id") or "-"),
                ("Global Person ID", detail.get("global_person_id") or "-"),
                ("Confidence", detail.get("confidence", "-")),
            ):
                layout.insertWidget(layout.indexOf(buttons), QLabel(f"{name}: {value}"))
            for label, content, _content_type, error in media_results:
                if error:
                    layout.insertWidget(layout.indexOf(buttons), QLabel(f"{label}: {error}"))
                    continue
                image = QPixmap()
                if not image.loadFromData(content):
                    layout.insertWidget(layout.indexOf(buttons), QLabel(f"{label}: 이미지 형식이 올바르지 않습니다."))
                    continue
                image_label = QLabel()
                image_label.setPixmap(image.scaledToWidth(440, Qt.SmoothTransformation))
                image_label.setToolTip(label)
                layout.insertWidget(layout.indexOf(buttons), image_label)
            for recording_id, recording, playback, error in recording_results:
                if error:
                    layout.insertWidget(layout.indexOf(buttons), QLabel(f"관련 녹화 {recording_id}: {error}"))
                    continue
                status = recording.get("status", "unknown")
                layout.insertWidget(layout.indexOf(buttons), QLabel(
                    f"관련 녹화 ID: {recording_id} | "
                    f"시작: {recording.get('start_time', '-')} | "
                    f"종료: {recording.get('end_time', '-')} | "
                    f"상태: {_RECORDING_STATUS_LABELS.get(status, status)}"
                ))
                playback_url = (playback or {}).get("playback_url")
                if playback_url:
                    button = QPushButton("녹화 재생")
                    button.clicked.connect(
                        lambda _checked=False, url=playback_url: self._open_playback_url(url)
                    )
                    layout.insertWidget(layout.indexOf(buttons), button)

        def failed(message):
            if state["open"] and dialog.isVisible():
                loading.setText(f"이벤트 정보를 불러오지 못했습니다: {message}")

        task = BackgroundTask(operation, self)
        tasks = getattr(self, "_event_detail_tasks", set())
        tasks.add(task)
        self._event_detail_tasks = tasks
        task.result.connect(render)
        task.failed.connect(failed)
        task.finished.connect(lambda: self._finish_event_detail_task(task))
        task.start()
        dialog.exec_()

    def _finish_event_detail_task(self, task):
        self._event_detail_tasks.discard(task)
        task.deleteLater()

    def _open_playback_url(self, playback_url):
        try:
            if not QDesktopServices.openUrl(QUrl(self.api.playback_url(playback_url))):
                raise ValueError("재생 프로그램을 열 수 없습니다.")
        except Exception as exc:
            QMessageBox.warning(self, "녹화 재생", f"재생 주소를 열 수 없습니다: {safe_error(exc)}")

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
    parser.add_argument("--allow-insecure-http", action="store_true")
    args = parser.parse_args(argv)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    window = CCTVMainWindow(
        args.server_url,
        ca_file=args.ca_file,
        allow_insecure_http=args.allow_insecure_http,
    )
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
