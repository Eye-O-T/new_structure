"""Reference main-window flow with server-backed operations and safe Qt lifetimes."""

import argparse
import logging
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError

from PyQt5.QtCore import QTimer, Qt, QUrl
from PyQt5.QtGui import QDesktopServices, QPixmap
from PyQt5.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from .legacy_gui import (
    CCTVMainWindow as ReferenceWindow,
    format_analysis_completed_description,
)
from .api import DesktopApi, safe_error
from .camera_view import CameraView
from .display_time import format_kst
from .media import PlaybackBridge
from .session_store import WindowsSessionStore
from .video_worker import VideoWorker
from server.desktop.common.tasks import BackgroundTask
from .local_camera import LocalCameraPublisher, rtsp_publish_url
from server.operations.local_camera import (
    load_device_setting,
    load_local_camera_enabled,
    publisher_status,
)


LOGGER = logging.getLogger(__name__)
_RECORDING_STATUS_LABELS = {
    "ready": "재생 가능",
    "writing": "처리 중",
    "missing": "파일 없음",
    "corrupt": "손상됨",
    "deleting": "삭제 중",
    "deleted": "삭제됨 / 보관기간 만료",
}
_CAMERA_RECORDING_LABELS = {
    "recording": "녹화 중",
    "waiting": "녹화 대기",
    "stopped": "녹화 중지",
    "error": "오류",
}
RECORDING_POLL_INTERVAL_MS = 2000
_ANALYSIS_STATUS_LABELS = {"pending": "대기 중", "running": "분석 중", "complete": "완료", "failed": "실패", "unconfigured": "분석기 미설정"}
_ANALYSIS_COLOR_LABELS = {"black": "검정", "white": "흰색", "gray": "회색", "red": "빨강", "orange": "주황", "yellow": "노랑", "green": "초록", "cyan": "청록", "blue": "파랑", "purple": "보라", "pink": "분홍", "brown": "갈색", "beige": "베이지"}
_ANALYSIS_QUALITY_FLAGS = {"mostly_dark": "대체로 어두움", "mostly_bright": "대체로 밝음", "low_contrast": "대비 낮음", "low_edge_energy": "윤곽 에너지 낮음"}
_ANALYSIS_ATTRIBUTE_LABELS = {
    "gender": "성별(gender)",
    "age": "연령대(age)",
    "hat": "모자(hat)",
    "upper_color": "상의 색상(upper_color)",
    "lower_color": "하의 색상(lower_color)",
    "sleeve": "소매(sleeve)",
    "lower_type": "하의 종류(lower_type)",
    "carrying": "소지품(carrying)",
}


def _format_attribute_item(entry) -> str | None:
    if isinstance(entry, dict):
        value = entry.get("value")
        if value is None:
            return None
        score = entry.get("score")
        if isinstance(score, (int, float)) and not isinstance(score, bool):
            return f"{value} ({score:.4f})"
        return str(value)
    if isinstance(entry, (str, int, float)) and not isinstance(entry, bool):
        return str(entry)
    return None


def _format_analysis_lines(detail: dict) -> list[str]:
    metadata = detail.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    analysis = metadata.get("analysis")
    analysis = analysis if isinstance(analysis, dict) else None
    lines: list[str] = []
    if analysis is None:
        lines.append("이 이벤트에 저장된 분석 정보가 없습니다.")
        return lines
    status = analysis.get("status")
    lines.append(f"상태: {_ANALYSIS_STATUS_LABELS.get(status, '알 수 없음')}")
    updated_at = analysis.get("updated_at") or detail.get("occurred_at")
    if updated_at:
        lines.append(
            f"{'분석 완료' if status == 'complete' else '상태 갱신'} "
            f"시각: {format_kst(updated_at)}"
        )
    result = analysis.get("result")
    result = result if isinstance(result, dict) else {}
    if status == "failed" and result.get("error_code"):
        lines.append(f"오류 코드: {result['error_code']}")
    if status == "complete":
        backend = result.get("backend")
        backend_version = result.get("backend_version")
        if isinstance(backend, str) and backend.strip():
            if isinstance(backend_version, str) and backend_version.strip():
                lines.append(f"분석기: {backend} ({backend_version})")
            else:
                lines.append(f"분석기: {backend}")
        image = result.get("image") if isinstance(result.get("image"), dict) else {}
        quality = image.get("quality") if isinstance(image.get("quality"), dict) else {}
        colors = result.get("clothing_colors")
        if isinstance(colors, dict):
            for key, title in (("upper", "상의 후보"), ("lower", "하의 후보")):
                region = colors.get(key) if isinstance(colors.get(key), dict) else {}
                dominant = (
                    region.get("dominant_color")
                    if isinstance(region.get("dominant_color"), dict)
                    else None
                )
                if dominant:
                    raw_name = dominant.get("name")
                    color = _ANALYSIS_COLOR_LABELS.get(
                        raw_name, str(raw_name or "알 수 없음")
                    )
                    rgb = dominant.get("rgb")
                    rgb_text = (
                        f"RGB {', '.join(map(str, rgb))}"
                        if isinstance(rgb, list) and len(rgb) == 3
                        else "RGB 제공 안 됨"
                    )
                    confidence = dominant.get("confidence")
                    score = (
                        f"신뢰 지표 {confidence:.4f}"
                        if isinstance(confidence, (int, float))
                        and not isinstance(confidence, bool)
                        else "신뢰 지표 제공 안 됨"
                    )
                    lines.append(f"{title}: {color} · {rgb_text} · {score}")
        attributes = result.get("attributes")
        if isinstance(attributes, dict):
            for attr_name, attr_value in attributes.items():
                title = _ANALYSIS_ATTRIBUTE_LABELS.get(attr_name, str(attr_name))
                if isinstance(attr_value, dict):
                    formatted = _format_attribute_item(attr_value)
                    if formatted:
                        lines.append(f"{title}: {formatted}")
                elif isinstance(attr_value, list):
                    items = [
                        text
                        for item in attr_value
                        if (text := _format_attribute_item(item))
                    ]
                    if items:
                        lines.append(f"{title}: {', '.join(items)}")
        sizes = [
            image.get(key)
            for key in ("width", "height", "sample_width", "sample_height")
        ]
        if all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in sizes
        ):
            lines.append(
                f"이미지 크기(원본 W×H, 표본 W×H): {sizes[0]} × {sizes[1]} · {sizes[2]} × {sizes[3]}"
            )
        elif all(
            isinstance(image.get(key), int) and not isinstance(image.get(key), bool)
            for key in ("width", "height")
        ):
            lines.append(
                f"이미지 크기(원본 W×H): {image['width']} × {image['height']}"
            )
        for key, title in (
            ("luminance_mean", "밝기 평균"),
            ("luminance_stddev", "밝기 표준편차"),
            ("laplacian_variance", "윤곽 에너지"),
        ):
            value = quality.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                lines.append(f"{title}: {value:.4f}")
        for key, title in (
            ("dark_pixel_fraction", "암부 픽셀 비율"),
            ("bright_pixel_fraction", "명부 픽셀 비율"),
        ):
            value = quality.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                lines.append(f"{title}: {value:.2%}")
        flags = quality.get("flags")
        if isinstance(flags, list):
            descriptions = [
                _ANALYSIS_QUALITY_FLAGS.get(flag, "알 수 없는 품질 플래그")
                for flag in flags
            ]
            lines.append(
                "품질 표시: "
                + (", ".join(descriptions) if descriptions else "해당 플래그 없음")
            )
        if isinstance(colors, dict) or quality:
            lines.append(
                "단일 사람 이미지의 후보 영역 측정값입니다. 신뢰 지표는 정답 확률이 아닙니다."
            )
        elif isinstance(attributes, dict) and attributes:
            lines.append(
                "단일 사람 이미지의 외관 속성 추정값입니다. 점수는 정답 확률이 아닙니다."
            )
    return lines


def _breakable_path(value: str) -> str:
    """긴 경로가 우측 패널 밖으로 잘리지 않도록 구분자 뒤에 줄바꿈 허용 문자(ZWSP)를 넣는다."""
    text = str(value or "")
    for sep in ("/", "\\", "_", "-"):
        text = text.replace(sep, sep + "\u200b")
    return text


class CCTVMainWindow(ReferenceWindow):
    def __init__(
        self,
        server_url="https://localhost",
        username="admin",
        storage_path="",
        ca_file=None,
        allow_insecure_http=False,
        rtsp_host="127.0.0.1",
        rtsp_port=8554,
    ):
        self.server_url, self.username, self.ca_file = server_url, username, ca_file
        self.allow_insecure_http = allow_insecure_http
        self.rtsp_host, self.rtsp_port = rtsp_host, rtsp_port
        self.session_store = (
            WindowsSessionStore(server_url) if sys.platform == "win32" else None
        )
        self.api = None
        self.cameras = []
        self._closing = False
        self._restore_task = None
        self._event_detail_tasks = set()
        self._playback_bridges = set()
        self._recording_tasks = set()
        self._recording_poll_task = None
        self._busy_recording_cameras = set()
        self._bulk_recording_busy = False
        self._camera_recordings = {}
        self._recording_notice = None
        self._stopping = False
        self._render_count = 0
        self._render_total_ms = 0.0
        self._last_render_log = time.monotonic()
        self._workers = {}
        self._local_camera_preview_requested = False
        self._local_camera_stopped_message = "노트북 카메라 녹화 및 송출이 중지되어 있습니다."
        self._views = {}
        self._camera_metrics = {}
        self._camera_events = {}
        self._cleared_event_ids = {}
        self._rendered_event_signatures = {}
        self._event_scroll_positions = {}
        self._rendered_event_camera_id = None
        self._last_coordinate_diagnostics = {}
        self._view_mode = "single"
        self._selected_camera_id = None
        self._updating_camera_picker = False
        self._monitoring = True
        super().__init__()
        self.ai_cctv_path = self.storage_root_path = str(storage_path)
        self.local_publisher = LocalCameraPublisher(
            self, data_root=self.storage_root_path or None
        )
        publisher = publisher_status(self.local_publisher.data_root)
        self._local_camera_preview_requested = (
            load_local_camera_enabled(self.local_publisher.data_root)
            and publisher.get("status") == "running"
            and publisher.get("camera_id") == "local-camera"
        )
        self.setMinimumSize(960, 600)
        self.video_label.setMinimumSize(320, 180)
        self.video_grid.removeWidget(self.video_label)
        self.video_label.hide()
        self._update_storage_label(storage_path)
        self.cam_status.setText("설정에서 로그인하세요.")
        self.cam_status.setWordWrap(True)
        for label in self.findChildren(QLabel):
            label.setTextFormat(Qt.PlainText)
            if label.text() == "카메라\nRTSP / LAN / USB 입력 상태":
                label.setText("카메라\n서버 연결 · 녹화 상태")
            if label.text() == "누적 추적":
                label.setText("관측 추적 (이번 시작)")
            if label.text() == "CAM-01 정문 · 실시간 분석 화면":
                self.camera_title = label
                label.setText("서버 카메라 · 실시간 영상")
        self._recording_poll_timer = QTimer(self)
        self._recording_poll_timer.setInterval(RECORDING_POLL_INTERVAL_MS)
        self._recording_poll_timer.timeout.connect(self.refresh_recording_states)
        self._update_recording_controls()
        QTimer.singleShot(0, self._restore_session)

    def _restore_session(self):
        if self._closing or self.session_store is None:
            return
        self.btn_start.setEnabled(False)
        self.btn_stop.setEnabled(False)
        self.btn_setting.setEnabled(False)
        url, ca_file = self.server_url, self.ca_file
        allow_insecure_http, store = self.allow_insecure_http, self.session_store

        def operation(_progress):
            api = DesktopApi(
                url,
                ca_file=ca_file,
                allow_insecure_http=allow_insecure_http,
                session_store=store,
            )
            if not api.restore_session():
                return None
            cameras = api.cameras()
            try:
                recording_items = api.cameras_recording().get("items", [])
            except Exception:
                recording_items = []
            return api, api.session_username, cameras, recording_items

        task = BackgroundTask(operation, self)
        self._restore_task = task

        def restored(result):
            if result is None:
                return
            self.api, self.username, self.cameras, recording_items = result
            self._ingest_cameras_recording_metadata(self.cameras)
            if recording_items:
                self._apply_recording_items(recording_items)
            available = self._available_camera_ids()
            if available:
                self._selected_camera_id = available[0]
                self.video_source = (self.api, available[0])
            self._monitoring = True
            self._refresh_camera_picker()
            self._show_views()
            self._update_recording_controls()
            self._recording_poll_timer.start()
            self.cam_status.setText("로그인 상태가 복원됐습니다.")

        def finished():
            self._restore_task = None
            self.btn_setting.setEnabled(True)
            self._update_recording_controls()
            task.deleteLater()
            if self._closing and not self._has_pending_background_tasks() and not self._workers:
                self.close()

        task.result.connect(restored)
        task.failed.connect(
            lambda _message: self.cam_status.setText(
                "로그인 상태를 복원하지 못했습니다. 설정에서 로그인하세요."
            )
        )
        task.finished.connect(finished)
        task.start()

    def _update_storage_label(self, storage_path: str) -> None:
        raw_path = str(storage_path or "서버에서 관리")
        self.storage_label.setText(
            f"서버 저장소\n{_breakable_path(raw_path)}\n\n"
            "실시간 영상은 창이 열려 있는 동안 자동 연결됩니다.\n"
            "녹화 시작·중지 설정은 창을 닫아도 서버에 유지됩니다."
        )
        self.storage_label.setToolTip(raw_path)

    def _camera_name(self, camera_id):
        for camera in self.cameras:
            if camera.get("camera_id") == camera_id:
                return str(camera.get("name") or camera_id)
        return camera_id

    def _available_camera_ids(self):
        return [
            str(camera["camera_id"])
            for camera in self.cameras
            if camera.get("enabled", True)
        ]

    def _ingest_cameras_recording_metadata(self, cameras):
        for camera in cameras or []:
            if not isinstance(camera, dict) or not camera.get("camera_id"):
                continue
            cid = str(camera["camera_id"])
            if "recording_state" in camera or "recording_enabled" in camera:
                rec_enabled = bool(camera.get("recording_enabled", False))
                rec_state = camera.get("recording_state") or (
                    "error"
                    if camera.get("recording_last_error_code")
                    else ("waiting" if rec_enabled else "stopped")
                )
                self._camera_recordings[cid] = {
                    "camera_id": cid,
                    "enabled": bool(camera.get("enabled", True)),
                    "recording_enabled": rec_enabled,
                    "recording_state": rec_state,
                    "last_error_code": camera.get("recording_last_error_code"),
                }

    def _camera_recording_info(self, camera_id):
        if camera_id in self._camera_recordings:
            return self._camera_recordings[camera_id]
        for camera in self.cameras:
            if camera.get("camera_id") == camera_id:
                rec_enabled = bool(camera.get("recording_enabled", False))
                rec_state = camera.get("recording_state") or (
                    "error"
                    if camera.get("recording_last_error_code")
                    else ("waiting" if rec_enabled else "stopped")
                )
                return {
                    "camera_id": camera_id,
                    "enabled": bool(camera.get("enabled", True)),
                    "recording_enabled": rec_enabled,
                    "recording_state": rec_state,
                    "last_error_code": camera.get("recording_last_error_code"),
                }
        return {
            "camera_id": camera_id,
            "enabled": True,
            "recording_enabled": False,
            "recording_state": "stopped",
            "last_error_code": None,
        }

    def _sync_view_recording_state(self, camera_id):
        view = self._views.get(camera_id)
        if view is None:
            return
        info = self._camera_recording_info(camera_id)
        busy = (
            self._bulk_recording_busy
            or camera_id in self._busy_recording_cameras
        )
        view.set_recording_state(
            info.get("recording_state", "stopped"),
            recording_enabled=info.get("recording_enabled", False),
            busy=busy,
            error_code=info.get("last_error_code") or info.get("error_code"),
            error_message=info.get("message"),
        )

    def _apply_recording_item(self, item):
        if not isinstance(item, dict) or not item.get("camera_id"):
            return
        camera_id = str(item["camera_id"])
        added_local_camera = camera_id == "local-camera" and not any(
            camera.get("camera_id") == camera_id for camera in self.cameras
        )
        if added_local_camera:
            self.cameras.append(
                {
                    "camera_id": camera_id,
                    "name": load_device_setting(self.local_publisher.data_root) or "노트북 카메라",
                    "enabled": bool(item.get("enabled", True)),
                }
            )
        normalized = {
            "camera_id": camera_id,
            "enabled": bool(item.get("enabled", True)),
            "recording_enabled": bool(item.get("recording_enabled", False)),
            "recording_state": str(item.get("recording_state") or (
                "waiting" if bool(item.get("recording_enabled", False)) else "stopped"
            )),
            "stream_ready": bool(item.get("stream_ready", False)),
            "recording_file_active": bool(item.get("recording_file_active", False)),
            "last_error_code": item.get("last_error_code") or item.get("error_code"),
            "message": item.get("message"),
        }
        self._camera_recordings[camera_id] = normalized
        for camera in self.cameras:
            if camera.get("camera_id") == camera_id:
                camera["recording_enabled"] = normalized["recording_enabled"]
                camera["recording_state"] = normalized["recording_state"]
                camera["recording_last_error_code"] = normalized["last_error_code"]
        self._sync_view_recording_state(camera_id)
        if not normalized["recording_enabled"]:
            worker = self._workers.get(camera_id)
            if worker is not None:
                worker.stop()
            view = self._views.get(camera_id)
            if view is not None:
                view.show_stopped("녹화 및 송출이 중지되어 있습니다.")
        elif camera_id != "local-camera":
            self._sync_workers(self._display_ids())
        self._update_recording_controls()
        if added_local_camera:
            self._refresh_camera_picker()
            self._show_views()
        if camera_id == self._selected_camera_id:
            self._update_selected_camera_status_banner()

    def _apply_recording_items(self, items):
        for item in items or []:
            if isinstance(item, dict) and item.get("camera_id"):
                self._apply_recording_item(item)
        self._update_recording_controls()

    def _update_selected_camera_status_banner(self):
        if self._recording_notice:
            self.cam_status.setText(self._recording_notice)
            self.cam_status.setToolTip(self._recording_notice)
            return
        camera_id = self._selected_camera_id
        if not camera_id:
            return
        info = self._camera_recording_info(camera_id)
        rec_state = info.get("recording_state", "stopped")
        rec_label = _CAMERA_RECORDING_LABELS.get(rec_state, "오류")
        view = self._views.get(camera_id)
        live_part = (
            "LIVE"
            if view is not None and view.status.text() == "LIVE"
            else "대기"
        )
        self.cam_status.setText(
            f"● {_breakable_path(camera_id)} · {live_part} · {rec_label}"
        )
        self.cam_status.setToolTip("")

    def _set_recording_notice(self, message):
        self._recording_notice = str(message) if message else None
        if self._recording_notice:
            self.cam_status.setText(self._recording_notice)
            self.cam_status.setToolTip(self._recording_notice)
        else:
            self._update_selected_camera_status_banner()

    def _update_recording_controls(self):
        local_running = (
            publisher_status(self.local_publisher.data_root).get("status")
            == "running"
        )
        local_enabled = load_local_camera_enabled(self.local_publisher.data_root)
        if self._restore_task is not None or self._bulk_recording_busy:
            self.btn_start.setEnabled(False)
            self.btn_stop.setEnabled(False)
            return
        available = self._available_camera_ids()
        if not self.api:
            self.btn_start.setEnabled(True)
            self.btn_stop.setEnabled(local_running)
            return
        if not available:
            self.btn_start.setEnabled(local_enabled)
            self.btn_stop.setEnabled(local_running)
            return
        states = [
            self._camera_recording_info(cid).get("recording_state", "stopped")
            for cid in available
        ]
        can_start = any(state in {"stopped", "error"} for state in states) or (
            local_enabled and not local_running
        )
        can_stop = local_running or any(
            state in {"recording", "waiting", "error"} for state in states
        )
        self.btn_start.setEnabled(can_start)
        self.btn_stop.setEnabled(can_stop)

    def _refresh_camera_picker(self):
        selected = self._selected_camera_id
        self._updating_camera_picker = True
        self.camera_picker.clear()
        for index, camera_id in enumerate(self._available_camera_ids()):
            display = f"{self._camera_name(camera_id)} · {camera_id}"
            self.camera_picker.addItem(display, camera_id)
            self.camera_picker.setItemData(index, display, Qt.ToolTipRole)
        index = self.camera_picker.findData(selected)
        if index >= 0:
            self.camera_picker.setCurrentIndex(index)
        self.camera_picker.setToolTip(self.camera_picker.currentText())
        self._updating_camera_picker = False
        enabled = bool(self._available_camera_ids())
        self.camera_picker.setEnabled(enabled)
        self.btn_grid.setEnabled(enabled)
        self._update_recording_controls()

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
                view.recording_start_requested.connect(self.start_camera_recording)
                view.recording_stop_requested.connect(self.stop_camera_recording)
                self._views[camera_id] = view
            view.set_overlay_enabled(self._overlay_enabled)
            view.set_selected(camera_id == self._selected_camera_id)
            self._sync_view_recording_state(camera_id)
            if not self._camera_recording_requested(camera_id):
                message = (
                    "노트북 카메라 사용이 꺼져 있습니다."
                    if camera_id == "local-camera"
                    and not load_local_camera_enabled(self.local_publisher.data_root)
                    else self._local_camera_stopped_message
                    if camera_id == "local-camera"
                    else "녹화 및 송출이 중지되어 있습니다."
                )
                view.show_stopped(message)
            if self._view_mode == "grid":
                self.video_grid.addWidget(view, index // 2, index % 2)
            else:
                self.video_grid.addWidget(view, 0, 0)
            view.show()
        self.camera_title.setText(
            "서버 카메라 · 2×2 실시간 영상"
            if self._view_mode == "grid"
            else f"{self._selected_camera_id or '카메라'} · 실시간 영상"
        )
        self._sync_workers(camera_ids)

    def _sync_workers(self, camera_ids):
        wanted = set(camera_ids)
        for camera_id, worker in list(self._workers.items()):
            if camera_id not in wanted or not self._camera_recording_requested(camera_id):
                worker.stop()
        if not self._monitoring or not self.api or self._closing or self._stopping:
            return
        for camera_id in camera_ids:
            if (
                camera_id not in self._workers
                and self._camera_recording_requested(camera_id)
            ):
                self._start_camera_worker(camera_id)

    def _camera_recording_requested(self, camera_id):
        if camera_id in self._busy_recording_cameras:
            return False
        recording_requested = bool(
            self._camera_recording_info(camera_id).get("recording_enabled", False)
        )
        if camera_id == "local-camera":
            return self._local_camera_preview_requested and recording_requested
        return recording_requested

    def pause_local_camera_preview(self, message="노트북 카메라 녹화 및 송출이 중지되어 있습니다."):
        self._local_camera_preview_requested = False
        worker = self._workers.get("local-camera")
        if worker is not None:
            worker.stop()
        self._set_local_camera_stopped_message(message)

    def _set_local_camera_stopped_message(self, message):
        self._local_camera_stopped_message = message
        view = self._views.get("local-camera")
        if view is not None:
            view.show_stopped(message)

    def _resume_local_camera_preview(self):
        data_root = self.local_publisher.data_root
        if (
            not load_local_camera_enabled(data_root)
            or publisher_status(data_root).get("status") != "running"
        ):
            return
        self._local_camera_preview_requested = True
        self._local_camera_stopped_message = ""
        if not any(item.get("camera_id") == "local-camera" for item in self.cameras):
            self.cameras.append(
                {
                    "camera_id": "local-camera",
                    "name": load_device_setting(data_root) or "노트북 카메라",
                    "enabled": True,
                }
            )
        self._refresh_camera_picker()
        self._show_views()

    def _start_camera_worker(self, camera_id):
        if not self._camera_recording_requested(camera_id):
            return
        worker = VideoWorker(source=(self.api, camera_id))
        cleared_id = self._cleared_event_ids.get(camera_id, 0)
        if cleared_id and hasattr(worker, "clear_retained_events"):
            worker.clear_retained_events(cleared_id)
        self._workers[camera_id] = worker
        worker.frame_ready.connect(
            lambda frame, cid=camera_id, current=worker: self._on_frame(
                cid, current, frame
            )
        )
        worker.objects_ready.connect(
            lambda payload, cid=camera_id, current=worker: self._on_objects(
                cid, current, payload
            )
        )
        worker.metrics_ready.connect(
            lambda metrics, cid=camera_id, current=worker: self._on_metrics(
                cid, current, metrics
            )
        )
        worker.event_ready.connect(
            lambda event, cid=camera_id, current=worker: self._on_event(
                cid, current, event
            )
        )
        worker.loading_ready.connect(
            lambda message, cid=camera_id, current=worker: self._on_loading(
                cid, current, message
            )
        )
        worker.finished.connect(
            lambda cid=camera_id, current=worker: self._on_worker_finished(
                cid, current
            )
        )
        view = self._views.get(camera_id)
        if view:
            view.show_loading("서버 영상 연결 중…")
        worker.start()

    def _is_current_worker(self, camera_id, worker):
        return self._workers.get(camera_id) is worker

    def _accept_worker_data(self, camera_id, worker):
        return self._is_current_worker(camera_id, worker) and (
            self._camera_recording_requested(camera_id)
            and worker.running
        )

    def _on_frame(self, camera_id, worker, frame):
        try:
            if self._accept_worker_data(camera_id, worker) and not self._stopping:
                view = self._views.get(camera_id)
                if view:
                    view.set_frame(frame)
                if camera_id == self._selected_camera_id:
                    self._update_selected_camera_status_banner()
        finally:
            worker.acknowledge_frame()

    def _on_objects(self, camera_id, worker, payload):
        if not self._accept_worker_data(camera_id, worker):
            return
        view = self._views.get(camera_id)
        if view:
            view.set_objects(payload)

    def _on_metrics(self, camera_id, worker, metrics):
        if not self._accept_worker_data(camera_id, worker):
            return
        self._camera_metrics[camera_id] = metrics
        received = metrics.get("coordinate_received_monotonic")
        ui_delay = max(0, time.monotonic() - received) if isinstance(received, (int, float)) else None
        now = time.monotonic()
        if now - self._last_coordinate_diagnostics.get(camera_id, 0) >= 5:
            LOGGER.debug(
                "coordinate diagnostics camera=%s request_seconds=%s observed_age_seconds=%s ui_dispatch_seconds=%s",
                camera_id,
                metrics.get("coordinate_request_seconds"),
                metrics.get("coordinate_age_seconds"),
                ui_delay,
            )
            self._last_coordinate_diagnostics[camera_id] = now
        if camera_id == self._selected_camera_id:
            self._render_selected_summary()

    def _on_event(self, camera_id, worker, event):
        if not self._accept_worker_data(camera_id, worker):
            return
        event = {**event, "camera_id": camera_id}
        event_id = event.get("id")
        if event_id is not None:
            worker_min_id = getattr(worker, "_min_after_event_id", None)
            if worker_min_id == 0 and self._cleared_event_ids.get(camera_id, 0) > 0:
                self._cleared_event_ids[camera_id] = 0
            try:
                if int(event_id) <= self._cleared_event_ids.get(camera_id, 0):
                    return
            except (TypeError, ValueError):
                pass
            for existing in self._camera_events.get(camera_id, []):
                if str(existing.get("id")) == str(event_id):
                    for key in ("person_id", "global_person_id", "gender", "age"):
                        if event.get(key) is not None:
                            existing[key] = event[key]
                    if isinstance(event.get("metadata"), dict) and event["metadata"]:
                        existing["metadata"] = dict(event["metadata"])
                    if camera_id == self._selected_camera_id:
                        self._render_selected_summary()
                    return
        session_id = event.get("tracking_session_id")
        if (
            session_id
            and event.get("person_id") is not None
            and event.get("global_person_id") is not None
        ):
            for existing in self._camera_events.get(camera_id, []):
                if (
                    existing.get("tracking_session_id") == session_id
                    and existing.get("person_id") == event.get("person_id")
                    and existing.get("global_person_id") is None
                ):
                    existing["global_person_id"] = event["global_person_id"]
        created_at = event.get("created_at")
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(created_at.replace("Z", "+00:00"))).total_seconds()
        except (ValueError, TypeError, AttributeError):
            age = None
        if event_id is not None:
            LOGGER.debug(
                "event timeline delivery camera=%s event_id=%s created_age_seconds=%s",
                camera_id, event_id, age,
            )
        self._camera_events.setdefault(camera_id, []).append(event)
        self._camera_events[camera_id].sort(key=self._event_sort_key, reverse=True)
        del self._camera_events[camera_id][30:]
        if event.get("type") in {"error", "network_failure", "objects_error"}:
            view = self._views.get(camera_id)
            if view and event.get("type") != "objects_error":
                view.show_error(event.get("message", "영상 연결 오류"))
        if camera_id == self._selected_camera_id:
            self._render_selected_summary()

    @staticmethod
    def _event_timestamp(event):
        value = event.get("occurred_at") or event.get("time")
        try:
            stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if stamp.tzinfo is None or stamp.utcoffset() is None:
                return float("-inf")
            return stamp.timestamp()
        except (ValueError, TypeError, AttributeError, OverflowError):
            return float("-inf")

    @classmethod
    def _event_sort_key(cls, event):
        try:
            event_id = int(event.get("id"))
        except (TypeError, ValueError, OverflowError):
            event_id = -1
        return cls._event_timestamp(event), event_id

    def _on_loading(self, camera_id, worker, message):
        if self._accept_worker_data(camera_id, worker):
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
            if camera_id == "local-camera" and not self._local_camera_preview_requested:
                view.show_stopped(self._local_camera_stopped_message)
            else:
                view.set_status("영상 연결 재시도 대기", "#facc15")
        if self._stopping and not self._workers:
            self._finish_stopping()
        elif (
            self._monitoring
            and self.api
            and not self._closing
            and self._camera_recording_requested(camera_id)
            and camera_id in self._display_ids()
        ):
            # Do not leave a tile blank when a deselected worker finishes after
            # that camera has already been selected again.
            self._start_camera_worker(camera_id)

    def _render_selected_summary(self):
        camera_id = self._selected_camera_id
        metrics = self._camera_metrics.get(camera_id, {})
        self.metric_current["value"].setText(str(metrics.get("current_objects", 0)))
        self.metric_total["value"].setText(str(metrics.get("tracked_total", 0)))
        events = self._camera_events.get(camera_id, [])
        signature = tuple(
            (
                str(event.get("id")),
                event.get("type"),
                event.get("time"),
                event.get("occurred_at"),
                event.get("person_id"),
                event.get("global_person_id"),
                format_analysis_completed_description(event)
                if (event.get("type") or event.get("event_type"))
                == "analysis_completed"
                else None,
                event.get("message"),
            )
            for event in events
        )
        if (
            self._rendered_event_camera_id == camera_id
            and self._rendered_event_signatures.get(camera_id) == signature
        ):
            return
        self._rendered_event_signatures[camera_id] = signature
        previous_camera_id = self._rendered_event_camera_id
        scroll_bar = self.event_scroll.verticalScrollBar()
        if previous_camera_id is not None:
            self._event_scroll_positions[previous_camera_id] = (
                scroll_bar.value(),
                scroll_bar.value() >= scroll_bar.maximum() - 1,
            )
        old_position, old_follow = self._event_scroll_positions.get(
            camera_id, (scroll_bar.maximum(), True)
        )
        if previous_camera_id == camera_id:
            old_follow = scroll_bar.value() >= scroll_bar.maximum() - 1
        self.appear_count = self.disappear_count = 0
        while self.event_list.count():
            item = self.event_list.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for event in reversed(events):
            ReferenceWindow.add_event(self, event)
        self._rendered_event_camera_id = camera_id
        QTimer.singleShot(
            0,
            lambda bar=scroll_bar, position=old_position, follow=old_follow: bar.setValue(
                bar.maximum() if follow else min(position, bar.maximum())
            ),
        )
        self.metric_appear["value"].setText(str(self.appear_count))
        self.metric_disappear["value"].setText(str(self.disappear_count))

    def clear_event_timeline(self):
        camera_id = self._selected_camera_id
        if camera_id:
            max_id = self._cleared_event_ids.get(camera_id, 0)
            for event in self._camera_events.get(camera_id, []):
                try:
                    max_id = max(max_id, int(event.get("id") or 0))
                except (TypeError, ValueError):
                    pass
            worker = self._workers.get(camera_id)
            if worker is not None and hasattr(worker, "clear_retained_events"):
                max_id = max(max_id, int(worker.clear_retained_events(max_id) or 0))
            self._cleared_event_ids[camera_id] = max_id
            self._camera_events[camera_id] = []
            self._rendered_event_signatures.pop(camera_id, None)
            self._event_scroll_positions.pop(camera_id, None)
        super().clear_event_timeline()
        if camera_id:
            self._render_selected_summary()

    def select_camera_from_picker(self, _index):
        if not self._updating_camera_picker:
            self.select_camera(self.camera_picker.currentData())

    def select_camera(self, camera_id):
        if not camera_id or camera_id == self._selected_camera_id:
            return
        self._set_recording_notice(None)
        self._selected_camera_id = camera_id
        self._refresh_camera_picker()
        self._show_views()
        self._render_selected_summary()
        self._update_selected_camera_status_banner()

    def open_single_camera(self, camera_id):
        self._set_recording_notice(None)
        self._selected_camera_id = camera_id
        self._view_mode = "single"
        self.btn_grid.setChecked(False)
        self._refresh_camera_picker()
        self._show_views()
        self._render_selected_summary()
        self._update_selected_camera_status_banner()

    def toggle_grid_view(self, checked):
        self._view_mode = "grid" if checked else "single"
        self._show_views()

    def cameras_updated(self, cameras):
        """Accept a settings-dialog refresh without disturbing unchanged views."""
        self.cameras = cameras
        self._ingest_cameras_recording_metadata(cameras)
        available = set(self._available_camera_ids())
        for camera_id, worker in list(self._workers.items()):
            if camera_id not in available:
                worker.stop()
        self._refresh_camera_picker()
        self._show_views()
        if self.api and available:
            self.refresh_recording_states()

    def refresh_recording_states(self):
        if (
            not self.api
            or self._closing
            or self._recording_poll_task is not None
            or self._recording_tasks
        ):
            return
        api = self.api

        def operation(_progress):
            return api.cameras_recording()

        task = BackgroundTask(operation, self)
        self._recording_poll_task = task

        def handle_result(payload):
            items = payload.get("items", []) if isinstance(payload, dict) else []
            self._apply_recording_items(items)

        def handle_finished():
            self._recording_poll_task = None
            task.deleteLater()
            if self._closing and not self._has_pending_background_tasks() and not self._workers:
                QTimer.singleShot(0, self.close)

        task.result.connect(handle_result)
        task.finished.connect(handle_finished)
        task.start()

    def _ensure_logged_in_for_recording(self):
        if not self.api:
            self.open_settings()
        if not self.api:
            return False
        if not self.cameras:
            self.cameras = self.api.cameras()
            self._ingest_cameras_recording_metadata(self.cameras)
        available = self._available_camera_ids()
        if not self._selected_camera_id and available:
            self._selected_camera_id = available[0]
        if not available and load_local_camera_enabled(self.local_publisher.data_root):
            self._selected_camera_id = "local-camera"
        self._monitoring = True
        self._refresh_camera_picker()
        self._show_views()
        local_setting = self.local_publisher.data_root
        return (
            bool(available)
            or load_local_camera_enabled(local_setting)
            or publisher_status(local_setting).get("status") == "running"
        )

    def start_all_recording(self):
        if not self._ensure_logged_in_for_recording():
            return
        self._run_bulk_recording_action(True)

    def stop_all_recording(self):
        if not self._ensure_logged_in_for_recording():
            return
        self._run_bulk_recording_action(False)

    def _start_local_camera_for_recording(self, api):
        data_root = self.local_publisher.data_root
        if not load_local_camera_enabled(data_root):
            raise RuntimeError("설정에서 노트북 카메라 사용을 켜세요.")
        device_name = load_device_setting(data_root)
        if not device_name:
            raise RuntimeError("노트북 카메라 사용이 켜져 있지만 장치가 선택되지 않았습니다.")
        current = publisher_status(data_root)
        already_running = (
            current.get("status") == "running"
            and current.get("camera_id") == "local-camera"
            and current.get("device_name") == device_name
        )
        previously_recording = bool(
            self._camera_recording_info("local-camera").get("recording_enabled")
        )
        if current.get("status") in {"running", "starting"} and not already_running:
            self.local_publisher.stop()
        if not already_running:
            try:
                registered = api.register_local_camera("local-camera", device_name)
            except Exception as exc:
                if getattr(exc, "status_code", None) != 409:
                    raise
                api.camera(
                    "local-camera", method="PATCH", payload={"name": device_name, "enabled": True}
                )
                registered = api.camera(
                    "local-camera", "/publish-credentials/rotate", "POST"
                )
            credentials = registered.get("publish_credentials") or {}
            username, password = credentials.get("username"), credentials.get("password")
            if not username or not password:
                raise RuntimeError("노트북 카메라 송출 계정을 발급받지 못했습니다.")
            stream_url = rtsp_publish_url(
                self.rtsp_host, self.rtsp_port, "local-camera", username, password
            )

        result = api.start_camera_recording("local-camera")
        started_here = False
        try:
            if not already_running:
                self.local_publisher.start(device_name, stream_url, camera_id="local-camera")
                started_here = True
            self._wait_for_local_camera(api)
        except Exception:
            if started_here and publisher_status(data_root).get("status") == "running":
                try:
                    self.local_publisher.stop()
                except Exception:
                    LOGGER.exception("Failed to stop local publisher after camera start failure")
            if not previously_recording:
                try:
                    api.stop_camera_recording("local-camera")
                except Exception:
                    LOGGER.exception("Failed to restore stopped state after local start failure")
            raise
        return result

    def _wait_for_local_camera(self, api):
        live = api.camera("local-camera", "/live?protocol=hls")
        playlist_path = api.media_path("local-camera", live["hls_url"])
        for _ in range(15):
            try:
                playlist = api.media("local-camera", playlist_path).decode("utf-8")
                if any(
                    line.strip() and not line.startswith("#")
                    for line in playlist.splitlines()
                ):
                    return
            except HTTPError as exc:
                if exc.code in {401, 403}:
                    raise RuntimeError(
                        "노트북 카메라 영상 인증이 거부됐습니다. 송출 계정과 서버 설정을 확인하세요."
                    ) from None
                if exc.code not in {404, 502, 503}:
                    raise
            time.sleep(1)
        raise RuntimeError(
            "15초 안에 노트북 카메라 영상이 서버에 도착하지 않았습니다. RTSP 송출과 인증 로그를 확인하세요."
        )

    def start_video(self):
        self.start_all_recording()

    def stop_video(self):
        self.stop_all_recording()

    def _perform_camera_recording_action(self, api, camera_id: str, start: bool):
        if start:
            if camera_id == "local-camera":
                return self._start_local_camera_for_recording(api)
            return api.start_camera_recording(camera_id)

        result = None
        errors = []
        try:
            result = api.stop_camera_recording(camera_id)
        except Exception as exc:
            errors.append(f"서버 중지 실패: {exc}")
        if camera_id == "local-camera" and publisher_status(
            self.local_publisher.data_root
        ).get("status") == "running":
            try:
                self.local_publisher.stop()
            except Exception as exc:
                errors.append(f"로컬 publisher 중지 실패: {exc}")
        if errors:
            raise RuntimeError("; ".join(errors))
        return result

    def _run_bulk_recording_action(self, start: bool):
        if not self.api or self._bulk_recording_busy or self._busy_recording_cameras:
            return
        api = self.api
        if start:
            camera_ids = self._available_camera_ids()
            if load_local_camera_enabled(self.local_publisher.data_root):
                camera_ids = list(dict.fromkeys([*camera_ids, "local-camera"]))
            else:
                camera_ids = [cid for cid in camera_ids if cid != "local-camera"]
        else:
            camera_ids = list(dict.fromkeys(
                str(item["camera_id"]) for item in self.cameras if item.get("camera_id")
            ))
            if publisher_status(self.local_publisher.data_root).get("status") == "running":
                camera_ids = list(dict.fromkeys([*camera_ids, "local-camera"]))
        if not camera_ids:
            self._set_recording_notice("제어할 카메라가 없습니다.")
            return

        self._bulk_recording_busy = True
        self._busy_recording_cameras.update(camera_ids)
        self._set_recording_notice(None)
        if not start:
            self.pause_local_camera_preview("전체 녹화 중지 요청을 적용하고 있습니다.")
            for camera_id in camera_ids:
                worker = self._workers.get(camera_id)
                if worker is not None:
                    worker.stop()
                view = self._views.get(camera_id)
                if view is not None:
                    view.show_stopped("녹화 및 송출을 중지하고 있습니다.")
        for camera_id in camera_ids:
            self._sync_view_recording_state(camera_id)
        self._update_recording_controls()

        def operation(_progress):
            items = []
            for camera_id in camera_ids:
                try:
                    item = self._perform_camera_recording_action(api, camera_id, start)
                    item = dict(item or {})
                    item.update(camera_id=camera_id, ok=True)
                except Exception as exc:
                    item = {
                        "camera_id": camera_id,
                        "ok": False,
                        "enabled": True,
                        "recording_enabled": False if not start else True,
                        "recording_state": "error",
                        "error_code": "RECORDING_CONTROL_FAILED",
                        "message": str(exc),
                    }
                    try:
                        item.update(api.camera_recording(camera_id))
                    except Exception:
                        pass
                    item.update(ok=False, error_code="RECORDING_CONTROL_FAILED", message=str(exc))
                items.append(item)
            failed = sum(not item.get("ok", False) for item in items)
            return {
                "items": items,
                "total": len(items),
                "succeeded": len(items) - failed,
                "failed": failed,
            }

        task = BackgroundTask(operation, self)
        self._recording_tasks.add(task)

        def handle_result(payload):
            self._apply_recording_items(payload.get("items", []))
            if start:
                self._resume_local_camera_preview()
            failures = [
                f"{item.get('camera_id')}: {item.get('message') or item.get('error_code') or '오류'}"
                for item in payload.get("items", []) if not item.get("ok", True)
            ]
            action = "시작" if start else "중지"
            if failures:
                self._set_recording_notice(
                    f"전체 녹화 {action} 일부 실패: " + " | ".join(failures)
                )
            else:
                self._set_recording_notice(f"전체 녹화 {action}를 적용했습니다.")

        def handle_failed(message):
            self._set_recording_notice(f"전체 녹화 제어 오류: {message}")

        def handle_finished():
            self._bulk_recording_busy = False
            self._busy_recording_cameras.difference_update(camera_ids)
            self._recording_tasks.discard(task)
            self._sync_workers(self._display_ids())
            for camera_id in camera_ids:
                self._sync_view_recording_state(camera_id)
            self._update_recording_controls()
            task.deleteLater()
            if self._closing and not self._has_pending_background_tasks() and not self._workers:
                QTimer.singleShot(0, self.close)

        task.result.connect(handle_result)
        task.failed.connect(handle_failed)
        task.finished.connect(handle_finished)
        task.start()

    def start_camera_recording(self, camera_id):
        self._run_single_camera_recording_action(camera_id, True)

    def stop_camera_recording(self, camera_id):
        self._run_single_camera_recording_action(camera_id, False)

    def _run_single_camera_recording_action(self, camera_id: str, start: bool):
        if (not self.api or not camera_id or self._bulk_recording_busy
                or camera_id in self._busy_recording_cameras):
            return
        api = self.api
        self._busy_recording_cameras.add(camera_id)
        if not start:
            if camera_id == "local-camera":
                self.pause_local_camera_preview("노트북 카메라 녹화 및 송출을 중지하고 있습니다.")
            worker = self._workers.get(camera_id)
            if worker is not None:
                worker.stop()
            view = self._views.get(camera_id)
            if view is not None:
                view.show_stopped("녹화 및 송출을 중지하고 있습니다.")
        self._set_recording_notice(None)
        self._sync_view_recording_state(camera_id)

        def operation(_progress):
            try:
                return self._perform_camera_recording_action(api, camera_id, start)
            except Exception as exc:
                try:
                    status = api.camera_recording(camera_id)
                except Exception:
                    status = {
                        "camera_id": camera_id,
                        "enabled": True,
                        "recording_enabled": False,
                        "recording_state": "error",
                    }
                status.update(ok=False, error_code="RECORDING_CONTROL_FAILED", message=str(exc))
                return status

        task = BackgroundTask(operation, self)
        self._recording_tasks.add(task)

        def handle_result(payload):
            if start and camera_id == "local-camera":
                self._resume_local_camera_preview()
            if isinstance(payload, dict):
                self._apply_recording_item(payload)
            if isinstance(payload, dict) and not payload.get("ok", True):
                self._set_recording_notice(
                    f"{camera_id} 녹화 제어 일부 실패: "
                    f"{payload.get('message') or payload.get('error_code') or '오류'}"
                )
            else:
                self._recording_notice = None
                self.cam_status.setToolTip("")

        def handle_failed(message):
            self._apply_recording_item(
                {
                    "camera_id": camera_id,
                    "enabled": True,
                    "recording_enabled": False if not start else True,
                    "recording_state": "error",
                    "error_code": "RECORDING_CONTROL_FAILED",
                    "message": message,
                }
            )
            self._set_recording_notice(f"{camera_id} 녹화 제어 오류: {message}")

        def handle_finished():
            self._busy_recording_cameras.discard(camera_id)
            self._recording_tasks.discard(task)
            self._sync_workers(self._display_ids())
            self._sync_view_recording_state(camera_id)
            self._update_recording_controls()
            task.deleteLater()
            if self._closing and not self._has_pending_background_tasks() and not self._workers:
                QTimer.singleShot(0, self.close)

        task.result.connect(handle_result)
        task.failed.connect(handle_failed)
        task.finished.connect(handle_finished)
        task.start()

    def stop_display_workers(self):
        """창 종료나 로그아웃 시 실시간 영상 화면 연결만 정리한다."""
        self._recording_poll_timer.stop()
        self.close_playback_bridges()
        if not self._workers:
            self._finish_stopping()
            return
        self._stopping = True
        self.cam_status.setText("영상 연결을 정리하고 있습니다…")
        for worker in list(self._workers.values()):
            worker.stop()

    def _finish_stopping(self):
        self._monitoring = False
        self._stopping = False
        self._update_recording_controls()
        if self._closing and not self._has_pending_background_tasks():
            QTimer.singleShot(0, self.close)

    def set_bounding_boxes_enabled(self, enabled):
        super().set_bounding_boxes_enabled(enabled)
        for view in self._views.values():
            view.set_overlay_enabled(enabled)

    def open_settings(self):
        if self.resource_monitor_window is not None:
            self.resource_monitor_window.close()
            if self.resource_monitor_window is not None:
                QMessageBox.information(
                    self, "상태 조회 중", "리소스 조회가 끝난 뒤 설정을 열어 주세요."
                )
                return
        super().open_settings()
        if self.api:
            self._monitoring = True
            self._ingest_cameras_recording_metadata(self.cameras)
            if isinstance(self.video_source, tuple):
                camera_id = self.video_source[1]
                self._selected_camera_id = camera_id
            elif not self._selected_camera_id:
                available = self._available_camera_ids()
                if available:
                    self._selected_camera_id = available[0]
            self._refresh_camera_picker()
            self._show_views()
            self._update_recording_controls()
            self._recording_poll_timer.start()
            self.refresh_recording_states()
            if self._selected_camera_id:
                self._update_selected_camera_status_banner()
        self._update_storage_label(self.ai_cctv_path)

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
        self._last_event_detail_dialog = dialog
        dialog.setWindowTitle(f"이벤트 상세: {event_id}")
        dialog.resize(560, 640)
        dialog.setMinimumSize(480, 360)
        outer_layout = QVBoxLayout(dialog)

        scroll = QScrollArea(dialog)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("border: none;")
        content_widget = QWidget()
        content_widget.setMinimumWidth(0)
        content_widget.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout = QVBoxLayout(content_widget)
        loading = QLabel("이벤트 정보를 불러오는 중입니다.")
        loading.setWordWrap(True)
        layout.addWidget(loading)
        layout.addStretch()
        scroll.setWidget(content_widget)
        outer_layout.addWidget(scroll, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(dialog.reject)
        outer_layout.addWidget(buttons)

        state = {"open": True, "busy": True, "detail": None}

        def mark_closed(*_args):
            state["open"] = False

        dialog.destroyed.connect(mark_closed)
        dialog.finished.connect(mark_closed)

        def _add_wrapped_label(text: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setWordWrap(True)
            lbl.setTextFormat(Qt.PlainText)
            layout.insertWidget(layout.count() - 1, lbl)
            return lbl

        def render_analysis_section(detail):
            analysis_title = QLabel("분석 결과")
            analysis_title.setStyleSheet("font-size: 16px; font-weight: bold;")
            layout.insertWidget(layout.count() - 1, analysis_title)
            analysis_layout = QVBoxLayout()
            layout.insertLayout(layout.count() - 1, analysis_layout)
            for line in _format_analysis_lines(detail):
                label = QLabel(line)
                label.setWordWrap(True)
                label.setTextFormat(Qt.PlainText)
                analysis_layout.addWidget(label)

        def operation(_progress):
            detail = self.api.event(event_id)
            if detail.get("event_type") == "analysis_completed":
                return detail, [], []
            media_results = []
            media = (
                detail.get("media") if isinstance(detail.get("media"), dict) else {}
            )
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
                        if recording.get("status") == "ready"
                        else None
                    )
                    recording_results.append((recording_id, recording, playback, None))
                except Exception as exc:
                    recording_results.append(
                        (recording_id, None, None, safe_error(exc))
                    )
            return detail, media_results, recording_results

        def render(result):
            if not state["open"]:
                return
            loading.deleteLater()
            detail, media_results, recording_results = result
            state["detail"] = detail
            state["busy"] = False
            metadata = detail.get("metadata")
            metadata = metadata if isinstance(metadata, dict) else {}
            is_analysis_completed = detail.get("event_type") == "analysis_completed"
            pid_value = (
                detail.get("person_id")
                if detail.get("person_id") is not None
                else "-"
            )
            gpid_value = (
                detail.get("global_person_id")
                if detail.get("global_person_id") is not None
                else "null"
            )
            if is_analysis_completed:
                render_analysis_section(detail)
                appearance_event_id = metadata.get("appearance_event_id")
                appearance_occurred_at = metadata.get("appearance_occurred_at")
                fields = (
                    ("Event ID", detail.get("id", event_id)),
                    ("이벤트 유형", detail.get("event_type", "-")),
                    ("카메라", detail.get("camera_id", "-")),
                    ("완료 시각", format_kst(detail.get("occurred_at"))),
                    (
                        "원본 등장 이벤트 ID",
                        appearance_event_id
                        if appearance_event_id is not None
                        else "-",
                    ),
                    (
                        "원본 등장 시각",
                        format_kst(appearance_occurred_at)
                        if appearance_occurred_at
                        else "-",
                    ),
                    ("pid", pid_value),
                    ("gpid", gpid_value),
                )
            else:
                fields = (
                    ("Event ID", detail.get("id", event_id)),
                    ("이벤트 유형", detail.get("event_type", "-")),
                    ("카메라", detail.get("camera_id", "-")),
                    ("발생 시각", format_kst(detail.get("occurred_at"))),
                    ("pid", pid_value),
                    ("gpid", gpid_value),
                    (
                        "감지 Confidence",
                        detail.get("confidence")
                        if detail.get("confidence") is not None
                        else "-",
                    ),
                )
            for name, value in fields:
                _add_wrapped_label(f"{name}: {value}")
            for label, content, _content_type, error in media_results:
                if error:
                    _add_wrapped_label(f"{label}: {error}")
                    continue
                image = QPixmap()
                if not image.loadFromData(content):
                    _add_wrapped_label(f"{label}: 이미지 형식이 올바르지 않습니다.")
                    continue
                image_label = QLabel()
                image_label.setPixmap(image.scaledToWidth(440, Qt.SmoothTransformation))
                image_label.setToolTip(label)
                layout.insertWidget(layout.count() - 1, image_label)
            for recording_id, recording, playback, error in recording_results:
                if error:
                    _add_wrapped_label(f"관련 녹화 {recording_id}: {error}")
                    continue
                status = recording.get("status", "unknown")
                _add_wrapped_label(
                    f"관련 녹화 ID: {recording_id} | "
                    f"시작: {format_kst(recording.get('start_time'))} | "
                    f"종료: {format_kst(recording.get('end_time'))} | "
                    f"상태: {_RECORDING_STATUS_LABELS.get(status, status)}"
                )
                playback_url = (playback or {}).get("playback_url")
                if playback_url:
                    button = QPushButton("녹화 재생")
                    button.clicked.connect(
                        lambda _checked=False, url=playback_url: self._open_playback_url(
                            url
                        )
                    )
                    layout.insertWidget(layout.count() - 1, button)

        def failed(message):
            if state["open"]:
                loading.setText(f"이벤트 정보를 불러오지 못했습니다: {message}")
            state["busy"] = False

        task = BackgroundTask(operation, self)
        tasks = getattr(self, "_event_detail_tasks", set())
        tasks.add(task)
        self._event_detail_tasks = tasks
        task.result.connect(render)
        task.failed.connect(failed)
        task.finished.connect(lambda: self._finish_event_detail_task(task))
        task.start()
        dialog.exec_()

    def _has_pending_background_tasks(self):
        return bool(
            self._restore_task is not None
            or self._recording_tasks
            or self._recording_poll_task is not None
            or getattr(self, "_event_detail_tasks", None)
        )

    def _finish_event_detail_task(self, task):
        self._event_detail_tasks.discard(task)
        task.deleteLater()
        if self._closing and not self._has_pending_background_tasks() and not self._workers:
            self.close()

    def close_playback_bridges(self):
        bridges = getattr(self, "_playback_bridges", None)
        if not bridges:
            return
        for bridge in list(bridges):
            bridges.discard(bridge)
            try:
                bridge.close()
            except Exception:
                LOGGER.debug("Failed to close playback bridge", exc_info=True)

    def _open_playback_url(self, playback_url):
        bridge = None
        try:
            if not self.api:
                raise ValueError("다시 로그인하세요.")
            bridge = PlaybackBridge(self.api, playback_url)
            bridge.start()
            bridges = getattr(self, "_playback_bridges", None)
            if bridges is None:
                bridges = set()
                self._playback_bridges = bridges
            bridges.add(bridge)
            if not QDesktopServices.openUrl(QUrl(bridge.url)):
                bridges.discard(bridge)
                bridge.close()
                raise ValueError("재생 프로그램을 열 수 없습니다.")
        except Exception as exc:
            if bridge is not None:
                getattr(self, "_playback_bridges", set()).discard(bridge)
                bridge.close()
            QMessageBox.warning(
                self, "녹화 재생", f"재생 주소를 열 수 없습니다: {safe_error(exc)}"
            )

    def closeEvent(self, event):
        if self.resource_monitor_window is not None:
            self.resource_monitor_window.close()
            if self.resource_monitor_window is not None:
                event.ignore()
                return
        self._closing = True
        self._recording_poll_timer.stop()
        self.close_playback_bridges()
        if self._workers:
            self.stop_display_workers()
            event.ignore()
            return
        if self._has_pending_background_tasks():
            event.ignore()
            return
        event.accept()


def main(argv=None):
    parser = argparse.ArgumentParser(description="AI CCTV PyQt 관리자")
    parser.add_argument("--server-url", default="https://localhost")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--storage-path", default="")
    parser.add_argument("--ca-file")
    parser.add_argument("--allow-insecure-http", action="store_true")
    parser.add_argument("--rtsp-host", default="127.0.0.1")
    parser.add_argument("--rtsp-port", type=int, default=8554)
    args = parser.parse_args(argv)
    app = QApplication.instance() or QApplication(sys.argv[:1])
    window = CCTVMainWindow(
        args.server_url,
        username=args.username,
        storage_path=args.storage_path,
        ca_file=args.ca_file,
        allow_insecure_http=args.allow_insecure_http,
        rtsp_host=args.rtsp_host,
        rtsp_port=args.rtsp_port,
    )
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
