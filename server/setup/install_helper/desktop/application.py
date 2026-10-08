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
from .camera_view import CameraView
from .video_worker import VideoWorker
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
        self._workers = {}
        self._views = {}
        self._camera_metrics = {}
        self._camera_events = {}
        self._view_mode = "single"
        self._selected_camera_id = None
        self._updating_camera_picker = False
        self._monitoring = False
        super().__init__()
        self.ai_cctv_path = self.storage_root_path = str(storage_path)
        self.setMinimumSize(900, 600)
        self.video_label.setMinimumSize(320, 180)
        self.video_grid.removeWidget(self.video_label)
        self.video_label.hide()
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

    def _camera_name(self, camera_id):
        for camera in self.cameras:
            if camera.get("camera_id") == camera_id:
                return str(camera.get("name") or camera_id)
        return camera_id

    def _available_camera_ids(self):
        return [str(camera["camera_id"]) for camera in self.cameras if camera.get("enabled", True)]

    def _refresh_camera_picker(self):
        selected = self._selected_camera_id
        self._updating_camera_picker = True
        self.camera_picker.clear()
        for camera_id in self._available_camera_ids():
            self.camera_picker.addItem(f"{self._camera_name(camera_id)} · {camera_id}", camera_id)
        index = self.camera_picker.findData(selected)
        if index >= 0:
            self.camera_picker.setCurrentIndex(index)
        self._updating_camera_picker = False
        enabled = bool(self._available_camera_ids())
        self.camera_picker.setEnabled(enabled)
        self.btn_grid.setEnabled(enabled)

    def _display_ids(self):
        available = self._available_camera_ids()
        if self._selected_camera_id not in available:
            self._selected_camera_id = available[0] if available else None
        if self._view_mode == "single" or not self._selected_camera_id:
            return [self._selected_camera_id] if self._selected_camera_id else []
        start = available.index(self._selected_camera_id)
        return (available[start:] + available[:start])[:4]

    def _clear_video_grid(self):
        while self.video_grid.count():
            item = self.video_grid.takeAt(0)
            widget = item.widget()
            if widget:
                widget.setParent(None)

    def _show_views(self):
        camera_ids = self._display_ids()
        self._clear_video_grid()
        for index, camera_id in enumerate(camera_ids):
            view = self._views.get(camera_id)
            if view is None:
                view = CameraView(camera_id, self._camera_name(camera_id), self)
                view.selected.connect(self.select_camera)
                view.activated.connect(self.open_single_camera)
                self._views[camera_id] = view
            view.set_overlay_enabled(self._overlay_enabled)
            view.set_selected(camera_id == self._selected_camera_id)
            if self._view_mode == "grid":
                self.video_grid.addWidget(view, index // 2, index % 2)
            else:
                self.video_grid.addWidget(view, 0, 0)
            view.show()
        self.camera_title.setText(
            "서버 카메라 · 2×2 실시간 영상" if self._view_mode == "grid"
            else f"{self._selected_camera_id or '카메라'} · 실시간 영상"
        )
        self._sync_workers(camera_ids)

    def _sync_workers(self, camera_ids):
        wanted = set(camera_ids)
        for camera_id, worker in list(self._workers.items()):
            if camera_id not in wanted:
                worker.stop()
        if not self._monitoring:
            return
        for camera_id in camera_ids:
            if camera_id not in self._workers:
                self._start_camera_worker(camera_id)

    def _start_camera_worker(self, camera_id):
        worker = VideoWorker(source=(self.api, camera_id))
        self._workers[camera_id] = worker
        worker.frame_ready.connect(lambda frame, cid=camera_id, current=worker: self._on_frame(cid, current, frame))
        worker.objects_ready.connect(lambda payload, cid=camera_id, current=worker: self._on_objects(cid, current, payload))
        worker.metrics_ready.connect(lambda metrics, cid=camera_id, current=worker: self._on_metrics(cid, current, metrics))
        worker.event_ready.connect(lambda event, cid=camera_id, current=worker: self._on_event(cid, current, event))
        worker.loading_ready.connect(lambda message, cid=camera_id, current=worker: self._on_loading(cid, current, message))
        worker.finished.connect(lambda cid=camera_id, current=worker: self._on_worker_finished(cid, current))
        view = self._views.get(camera_id)
        if view:
            view.show_loading("서버 영상 연결 중…")
        worker.start()

    def _is_current_worker(self, camera_id, worker):
        return self._workers.get(camera_id) is worker

    def _on_frame(self, camera_id, worker, frame):
        try:
            if self._is_current_worker(camera_id, worker) and not self._stopping:
                view = self._views.get(camera_id)
                if view:
                    view.set_frame(frame)
                if camera_id == self._selected_camera_id:
                    self.cam_status.setText(f"● {camera_id} · LIVE")
        finally:
            worker.acknowledge_frame()

    def _on_objects(self, camera_id, worker, payload):
        if not self._is_current_worker(camera_id, worker):
            return
        view = self._views.get(camera_id)
        if view:
            view.set_objects(payload)

    def _on_metrics(self, camera_id, worker, metrics):
        if not self._is_current_worker(camera_id, worker):
            return
        self._camera_metrics[camera_id] = metrics
        if camera_id == self._selected_camera_id:
            self._render_selected_summary()

    def _on_event(self, camera_id, worker, event):
        if not self._is_current_worker(camera_id, worker):
            return
        event = {**event, "camera_id": camera_id}
        self._camera_events.setdefault(camera_id, []).insert(0, event)
        del self._camera_events[camera_id][30:]
        if event.get("type") in {"error", "network_failure"}:
            view = self._views.get(camera_id)
            if view:
                view.show_error(event.get("message", "영상 연결 오류"))
        if camera_id == self._selected_camera_id:
            self._render_selected_summary()

    def _on_loading(self, camera_id, worker, message):
        if self._is_current_worker(camera_id, worker):
            view = self._views.get(camera_id)
            if view:
                view.show_loading(message)

    def _on_worker_finished(self, camera_id, worker):
        if not self._is_current_worker(camera_id, worker):
            worker.deleteLater()
            return
        self._workers.pop(camera_id, None)
        worker.deleteLater()
        view = self._views.get(camera_id)
        if view and not self._stopping:
            view.set_status("연결 종료 — 재시도하려면 시작", "#ef4444")
        if self._stopping and not self._workers:
            self._finish_stopping()
        elif self._monitoring and camera_id in self._display_ids():
            # Do not leave a tile blank when a deselected worker finishes after
            # that camera has already been selected again.
            self._start_camera_worker(camera_id)

    def _render_selected_summary(self):
        camera_id = self._selected_camera_id
        metrics = self._camera_metrics.get(camera_id, {})
        self.metric_current["value"].setText(str(metrics.get("current_objects", 0)))
        self.metric_total["value"].setText(str(metrics.get("tracked_total", 0)))
        events = self._camera_events.get(camera_id, [])
        self.appear_count = self.disappear_count = 0
        while self.event_list.count():
            item = self.event_list.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for event in reversed(events):
            ReferenceWindow.add_event(self, event)
        self.metric_appear["value"].setText(str(self.appear_count))
        self.metric_disappear["value"].setText(str(self.disappear_count))

    def select_camera_from_picker(self, _index):
        if not self._updating_camera_picker:
            self.select_camera(self.camera_picker.currentData())

    def select_camera(self, camera_id):
        if not camera_id or camera_id == self._selected_camera_id:
            return
        self._selected_camera_id = camera_id
        self._refresh_camera_picker()
        self._show_views()
        self._render_selected_summary()

    def open_single_camera(self, camera_id):
        self._selected_camera_id = camera_id
        self._view_mode = "single"
        self.btn_grid.setChecked(False)
        self._refresh_camera_picker()
        self._show_views()
        self._render_selected_summary()

    def toggle_grid_view(self, checked):
        self._view_mode = "grid" if checked else "single"
        self._show_views()

    def cameras_updated(self, cameras):
        """Accept a settings-dialog refresh without disturbing unchanged views."""
        self.cameras = cameras
        available = set(self._available_camera_ids())
        for camera_id, worker in list(self._workers.items()):
            if camera_id not in available:
                worker.stop()
        self._refresh_camera_picker()
        self._show_views()

    def start_video(self):
        if not self.api or not isinstance(self.video_source, tuple) or self.video_source[0] is not self.api:
            self.open_settings()
        if not self.api or not isinstance(self.video_source, tuple) or self.video_source[0] is not self.api:
            return
        if not self.cameras:
            self.cameras = self.api.cameras()
        if not self._selected_camera_id:
            self._selected_camera_id = self.video_source[1]
        self._monitoring = True
        self._stopping = False
        self._refresh_camera_picker()
        self._show_views()
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(True)
        self.btn_setting.setEnabled(False)

    def stop_video(self):
        if not self._workers:
            self._finish_stopping()
            return
        self._stopping = True
        self.btn_stop.setEnabled(False)
        self.cam_status.setText("영상 연결을 정리하고 있습니다…")
        for worker in list(self._workers.values()):
            worker.stop()

    def _finish_stopping(self):
        self._monitoring = False
        self._stopping = False
        self.btn_start.setEnabled(True)
        self.btn_stop.setEnabled(False)
        self.btn_setting.setEnabled(True)
        self.cam_status.setText("모니터링 중지됨")
        for view in self._views.values():
            view.set_status("중지됨", "#ef4444")
        if self._closing:
            QTimer.singleShot(0, self.close)

    def set_bounding_boxes_enabled(self, enabled):
        super().set_bounding_boxes_enabled(enabled)
        for view in self._views.values():
            view.set_overlay_enabled(enabled)

    def open_settings(self):
        if self._workers:
            return
        if self.resource_monitor_window is not None:
            self.resource_monitor_window.close()
            if self.resource_monitor_window is not None:
                QMessageBox.information(self, "상태 조회 중", "리소스 조회가 끝난 뒤 설정을 열어 주세요.")
                return
        super().open_settings()
        if isinstance(self.video_source, tuple):
            camera_id = self.video_source[1]
            self._selected_camera_id = camera_id
            self._refresh_camera_picker()
            self._show_views()
            self.cam_status.setText(f"● {camera_id} · 준비됨")
        self.storage_label.setText(f"서버 저장소\n{self.ai_cctv_path or '서버에서 관리'}\n\n모니터링 중지는 화면 연결만 종료합니다.\n서버 녹화는 계속됩니다.")

    def open_resource_monitor(self):
        if not self.api:
            self.open_settings()
        if self.api:
            super().open_resource_monitor()

    def _build_resource_monitor_url(self, source):
        return self.server_url

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
        if self._workers:
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
