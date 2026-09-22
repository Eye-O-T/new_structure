"""Preserve VideoWorker's constructor/signals/start/stop contract; inference stays on the server."""

import threading
from datetime import datetime, timezone

from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtGui import QImage

from .api import safe_error
from .media import MediaBridge


def legacy_event(event):
    return {
        "type": {"person_appeared": "appear", "person_disappeared": "disappear"}.get(event["event_type"], event["event_type"]),
        "person_id": event.get("global_person_id") or event.get("person_id"),
        "time": event["occurred_at"],
    }


class ObservationAdapter:
    def __init__(self):
        self.tracks = set()

    def metrics(self, payload):
        observed = payload.get("observed_at")
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(observed.replace("Z", "+00:00"))).total_seconds()
            fresh = 0 <= age <= 5
        except (ValueError, TypeError, AttributeError):
            fresh = False
        objects = payload.get("objects", []) if fresh else []
        session = payload.get("tracking_session_id")
        self.tracks.update((session, item["person_id"]) for item in objects)
        return {"current_objects": len(objects) if fresh else "—", "tracked_total": len(self.tracks)}


class VideoWorker(QThread):
    frame_ready = pyqtSignal(object)
    metrics_ready = pyqtSignal(dict)
    event_ready = pyqtSignal(dict)
    loading_ready = pyqtSignal(str)

    def __init__(self, source=0, use_yolo=True, use_vlm=False, ai_cctv_path="",
                 original_segment_seconds=10, clip_max_seconds=10):
        super().__init__()
        self.source = source  # (DesktopApi, camera_id), instead of local webcam/RTSP input.
        self.running = True
        self._stop = threading.Event()
        self._frame_pending = threading.Event()

    def acknowledge_frame(self):
        self._frame_pending.clear()

    def stop(self):
        self.running = False
        self._stop.set()

    def poll(self):
        api, camera_id = self.source
        adapter = ObservationAdapter()
        start = datetime.now(timezone.utc).isoformat()
        seen = set()
        failed = False
        while not self._stop.is_set():
            try:
                self.metrics_ready.emit(adapter.metrics(api.camera(camera_id, "/objects")))
                end = datetime.now(timezone.utc).isoformat()
                cursor = None
                current = set()
                while not self._stop.is_set():
                    page = api.events(camera_id, start, cursor)
                    for event in page.get("items", []):
                        key = str(event["id"])
                        current.add(key)
                        if key not in seen:
                            self.event_ready.emit(legacy_event(event))
                    cursor = page.get("next_cursor")
                    if not page.get("has_more") or not cursor:
                        break
                seen = current
                start = end  # overlap at request start to include events arriving during pagination.
                failed = False
            except Exception as exc:
                self.metrics_ready.emit({"current_objects": "—", "tracked_total": len(adapter.tracks)})
                if not failed:
                    self.event_ready.emit({"type": "error", "message": safe_error(exc)})
                failed = True
            self._stop.wait(2)

    def run(self):
        api, camera_id = self.source
        poller = threading.Thread(target=self.poll)
        poller.start()
        try:
            import av

            while not self._stop.is_set():
                self.loading_ready.emit("서버 영상 연결 중...")
                try:
                    live = api.camera(camera_id, "/live")
                    with MediaBridge(api, camera_id, live["hls_url"]) as bridge:
                        with av.open(bridge.url, timeout=(8, 8), options={"protocol_whitelist": "http,tcp,crypto"}) as stream:
                            for frame in stream.decode(video=0):
                                if self._stop.is_set():
                                    break
                                if self._frame_pending.is_set():
                                    continue
                                rgb = frame.reformat(format="rgb24")
                                image = QImage(bytes(rgb.planes[0]), rgb.width, rgb.height,
                                               rgb.planes[0].line_size, QImage.Format_RGB888).copy()
                                self._frame_pending.set()
                                self.frame_ready.emit(image)
                    if not self._stop.is_set():
                        raise RuntimeError("Stream ended")
                except Exception:
                    if not self._stop.is_set():
                        self.event_ready.emit({"type": "network_failure", "message": "영상 연결을 확인하고 재접속합니다."})
                        self._stop.wait(3)
        except Exception as exc:
            self.event_ready.emit({"type": "error", "message": safe_error(exc)})
        finally:
            self.running = False
            self._stop.set()
            poller.join()
