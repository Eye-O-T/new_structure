"""Preserve VideoWorker's constructor/signals/start/stop contract; inference stays on the server."""

import threading
import time
import logging
from queue import Empty, Full, Queue
from datetime import datetime, timezone

from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtGui import QImage

from .api import safe_error
from .media import MediaBridge


LOGGER = logging.getLogger(__name__)
LIVE_FRAME_QUEUE_SIZE = 30  # One complete HLS segment at the default 30 fps.
LIVE_OPEN_OPTIONS = {
    "protocol_whitelist": "http,tcp,crypto",
    # FFmpeg defaults to -3: with 2s segments this starts about 6s behind live.
    "live_start_index": "-1",
    "analyzeduration": "100000",
    "probesize": "262144",
}


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
        self._decode_thread = None
        self._stats_lock = threading.Lock()
        self._decoded_frames = 0
        self._displayed_frames = 0
        self._dropped_frames = 0
        self._last_stats_log = 0.0

    def acknowledge_frame(self):
        self._frame_pending.clear()
        with self._stats_lock:
            self._displayed_frames += 1

    def _log_stats(self, queue_size=0, *, force=False):
        now = time.monotonic()
        if not force and now - self._last_stats_log < 5:
            return
        with self._stats_lock:
            decoded = self._decoded_frames
            displayed = self._displayed_frames
            dropped = self._dropped_frames
        LOGGER.info(
            "video diagnostics: decoded=%d displayed=%d dropped=%d queue=%d",
            decoded,
            displayed,
            dropped,
            queue_size,
        )
        self._last_stats_log = now

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
        frames = Queue(maxsize=LIVE_FRAME_QUEUE_SIZE)
        self._decode_thread = threading.Thread(
            target=self._decode_loop, args=(frames,), daemon=True
        )
        self._decode_thread.start()
        try:
            first_pts = None
            clock_start = None
            previous_pts = None
            session = None
            while not self._stop.is_set():
                # Select a frame only after Qt is ready, so a stalled UI cannot
                # hold an old frame outside the bounded queue.
                while self._frame_pending.is_set() and not self._stop.is_set():
                    self._stop.wait(0.005)
                if self._stop.is_set():
                    break
                try:
                    image, pts, frame_session, decoded_at = frames.get(timeout=0.2)
                except Empty:
                    continue
                now = time.monotonic()
                if now - decoded_at > 1:
                    with self._stats_lock:
                        self._dropped_frames += 1
                    continue
                if (frame_session != session or pts is None or first_pts is None
                        or previous_pts is None or pts < previous_pts
                        or pts - previous_pts > 0.5):
                    first_pts = pts
                    clock_start = now
                session = frame_session
                previous_pts = pts
                if pts is not None and clock_start is not None:
                    target = clock_start + (pts - first_pts)
                    delay = target - now
                    if delay > 0.5 or delay < -0.5:
                        # Re-anchor after queue drops, network stalls or PTS
                        # jumps instead of preserving an obsolete live clock.
                        first_pts, clock_start = pts, now
                    elif delay > 0:
                        self._stop.wait(delay)
                if self._stop.is_set():
                    break
                self._frame_pending.set()
                self.frame_ready.emit(image)
                self._log_stats(frames.qsize())
        except Exception as exc:
            self.event_ready.emit({"type": "error", "message": safe_error(exc)})
        finally:
            self.running = False
            self._stop.set()
            if self._decode_thread is not None:
                self._decode_thread.join(timeout=2)
            poller.join()

    def _decode_loop(self, frames):
        api, camera_id = self.source
        try:
            import av
        except Exception as exc:
            self.event_ready.emit({"type": "error", "message": safe_error(exc)})
            return
        while not self._stop.is_set():
            try:
                session = object()
                while True:
                    try:
                        frames.get_nowait()
                        with self._stats_lock:
                            self._dropped_frames += 1
                    except Empty:
                        break
                self.loading_ready.emit("서버 영상 연결 중...")
                live = api.camera(camera_id, "/live")
                with MediaBridge(api, camera_id, live["hls_url"]) as bridge:
                    with av.open(
                        bridge.url,
                        timeout=(8, 8),
                        options=LIVE_OPEN_OPTIONS,
                    ) as stream:
                        for frame in stream.decode(video=0):
                            if self._stop.is_set():
                                return
                            rgb = frame.reformat(format="rgb24")
                            image = QImage(
                                bytes(rgb.planes[0]), rgb.width, rgb.height,
                                rgb.planes[0].line_size, QImage.Format_RGB888
                            ).copy()
                            pts = frame.time
                            item = (image, float(pts) if pts is not None else None,
                                    session, time.monotonic())
                            with self._stats_lock:
                                self._decoded_frames += 1
                            while not self._stop.is_set():
                                try:
                                    # Never pace the decoder with the display:
                                    # keep reading HLS and evict old RGB frames.
                                    frames.put_nowait(item)
                                    break
                                except Full:
                                    try:
                                        frames.get_nowait()
                                        with self._stats_lock:
                                            self._dropped_frames += 1
                                    except Empty:
                                        pass
                if not self._stop.is_set():
                    raise RuntimeError("Stream ended")
            except Exception as exc:
                if not self._stop.is_set():
                    self.event_ready.emit({
                        "type": "network_failure",
                        "message": f"영상 연결 오류: {safe_error(exc)}\n3초 후 재접속합니다.",
                    })
                self._stop.wait(3)
        self._log_stats(frames.qsize(), force=True)
