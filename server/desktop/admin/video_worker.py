"""Preserve VideoWorker's constructor/signals/start/stop contract; inference stays on the server."""

import threading
import time
import logging
import re
from contextlib import contextmanager
from queue import Empty, Full, Queue
from datetime import datetime, timezone
from urllib.error import HTTPError

from PyQt5.QtCore import QThread, pyqtSignal
from PyQt5.QtGui import QImage

from .api import ServerApiError, safe_error
from .media import MediaBridge


LOGGER = logging.getLogger(__name__)


class CameraOfflineError(RuntimeError):
    """The camera is registered, but no media is being published."""


LIVE_FRAME_QUEUE_SIZE = 30  # One complete HLS segment at the default 30 fps.
LIVE_OPEN_OPTIONS = {
    "protocol_whitelist": "http,tcp,crypto",
    # FFmpeg defaults to -3: with 2s segments this starts about 6s behind live.
    "live_start_index": "-1",
    "analyzeduration": "100000",
    "probesize": "262144",
}
LIVE_RTSP_OPTIONS = {
    "rtsp_transport": "tcp",
    "fflags": "nobuffer",
    "flags": "low_delay",
    "reorder_queue_size": "0",
    "max_delay": "0",
    "analyzeduration": "100000",
    "probesize": "262144",
}
OBJECT_POLL_INTERVAL_SECONDS = 0.2
EVENT_POLL_INTERVAL_SECONDS = 0.5
NULL_GPID_REFRESH_INTERVAL_SECONDS = 5.0
POLL_REQUEST_TIMEOUT_SECONDS = 10.0


def _safe_stream_error(exc):
    """Return useful diagnostics without logging credentials or bearer tokens."""
    message = str(exc) or exc.__class__.__name__
    message = re.sub(
        r"([A-Za-z][A-Za-z0-9+.-]*://)([^/@\s]+):([^/@\s]+)@",
        r"\1<redacted>@",
        message,
    )
    message = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1<redacted>", message)
    return message


def _rtsp_failure_reason(exc, elapsed_ms):
    if isinstance(exc, ServerApiError):
        return "live API authentication rejected" if exc.status_code == 401 else "live API request failed"
    if type(exc).__name__ == "HTTPUnauthorizedError":
        return "RTSP authentication rejected"
    if type(exc).__name__ == "ExitError" and elapsed_ms >= 7_500:
        return "connection timeout"
    return type(exc).__name__


@contextmanager
def _open_live_stream(api, camera_id, av, *, stop_event=None):
    """Prefer direct RTSP and retain HLS as a compatibility fallback."""
    if stop_event is not None and stop_event.is_set():
        raise InterruptedError("Stream stop requested")
    rtsp_started_at = time.monotonic()
    stage = "live API request"
    try:
        live = api.camera(camera_id, "/live?protocol=rtsp")
        stage = "RTSP connection or decode"
        with av.open(live["url"], timeout=(8, 8), options=LIVE_RTSP_OPTIONS) as stream:
            yield stream
            return
    except Exception as exc:
        if stop_event is not None and stop_event.is_set():
            raise
        if type(exc).__name__ == "HTTPNotFoundError":
            live = api.camera(camera_id, "/live?protocol=hls")
            path = api.media_path(camera_id, live["hls_url"])
            try:
                api.media(camera_id, path)
            except HTTPError as media_error:
                if media_error.code == 404:
                    raise CameraOfflineError("카메라 영상이 송출되지 않고 있습니다.") from None
                raise
        elapsed_ms = (time.monotonic() - rtsp_started_at) * 1000
        error_status = getattr(exc, "status_code", None)
        error_code = getattr(exc, "code", None)
        LOGGER.warning(
            "RTSP unavailable camera=%s stage=%s reason=%s status=%s code=%s detail=%s; using HLS fallback",
            camera_id,
            stage,
            _rtsp_failure_reason(exc, elapsed_ms),
            error_status or "-",
            error_code or "-",
            _safe_stream_error(exc),
        )
        live = api.camera(camera_id, "/live?protocol=hls")
        with MediaBridge(api, camera_id, live["hls_url"]) as bridge:
            with av.open(
                bridge.url,
                timeout=(8, 8),
                options=LIVE_OPEN_OPTIONS,
            ) as stream:
                yield stream


def _extract_attribute_value(entry):
    if isinstance(entry, dict):
        value = entry.get("value")
        if isinstance(value, (str, int, float)) and not isinstance(value, bool):
            text = str(value).strip()
            return text or None
        return None
    if isinstance(entry, list):
        parts = [
            text
            for item in entry
            if (text := _extract_attribute_value(item))
        ]
        return ", ".join(parts) if parts else None
    if isinstance(entry, (str, int, float)) and not isinstance(entry, bool):
        text = str(entry).strip()
        return text or None
    return None


def _extract_analysis_attribute(metadata, key):
    if not isinstance(metadata, dict):
        return None
    analysis = metadata.get("analysis")
    if not isinstance(analysis, dict):
        return None
    result = analysis.get("result")
    if not isinstance(result, dict):
        return None
    attributes = result.get("attributes")
    if isinstance(attributes, dict) and key in attributes:
        value = _extract_attribute_value(attributes.get(key))
        if value:
            return value
    if key == "age" and "age_group" in result:
        return _extract_attribute_value(result.get("age_group"))
    return _extract_attribute_value(result.get(key))


def legacy_event(event):
    metadata = event.get("metadata")
    metadata = dict(metadata) if isinstance(metadata, dict) else {}
    return {
        "id": event.get("id"),
        "camera_id": event.get("camera_id"),
        "event_type": event.get("event_type"),
        "type": {
            "person_appeared": "appear",
            "person_disappeared": "disappear",
            "analysis_completed": "analysis_completed",
        }.get(event["event_type"], event["event_type"]),
        "person_id": event.get("person_id"),
        "global_person_id": event.get("global_person_id"),
        "gender": _extract_attribute_value(event.get("gender"))
        or _extract_analysis_attribute(metadata, "gender"),
        "age": _extract_attribute_value(event.get("age"))
        or _extract_analysis_attribute(metadata, "age"),
        "confidence": event.get("confidence"),
        "time": event["occurred_at"],
        "occurred_at": event.get("occurred_at"),
        "created_at": event.get("created_at"),
        "recording_segment_ids": event.get("recording_segment_ids", []),
        "media": event.get("media", {}),
        "tracking_session_id": metadata.get("tracking_session_id"),
        "appearance_event_id": metadata.get("appearance_event_id"),
        "appearance_occurred_at": metadata.get("appearance_occurred_at"),
        "analysis_job_id": metadata.get("analysis_job_id"),
        "metadata": metadata,
    }


def _event_sort_key(event):
    value = event.get("occurred_at") or event.get("time")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        ts = (
            float("-inf")
            if stamp.tzinfo is None or stamp.utcoffset() is None
            else stamp.timestamp()
        )
    except (ValueError, TypeError, AttributeError, OverflowError):
        ts = float("-inf")
    try:
        event_id = int(event.get("id"))
    except (TypeError, ValueError, OverflowError):
        event_id = -1
    return ts, event_id


def _upsert_retained_event(retained, item):
    event_id = item.get("id")
    if event_id is not None:
        for index, existing in enumerate(retained):
            if str(existing.get("id")) == str(event_id):
                updated = dict(existing)
                for key in ("person_id", "global_person_id", "gender", "age"):
                    if item.get(key) is not None:
                        updated[key] = item[key]
                if isinstance(item.get("metadata"), dict) and item["metadata"]:
                    updated["metadata"] = dict(item["metadata"])
                retained[index] = updated
                retained.sort(key=_event_sort_key, reverse=True)
                del retained[30:]
                return updated
    retained.append(dict(item))
    retained.sort(key=_event_sort_key, reverse=True)
    del retained[30:]
    return item


def _refresh_retained_event_ids(retained, item):
    event_id = item.get("id")
    if event_id is None:
        return None, False
    for existing in retained:
        if str(existing.get("id")) == str(event_id):
            changed = False
            for key in ("person_id", "global_person_id", "gender", "age"):
                if item.get(key) is not None and existing.get(key) != item.get(key):
                    existing[key] = item[key]
                    changed = True
            return existing, changed
    return None, False


def _has_unresolved_analysis_event(retained):
    return any(
        (
            item.get("event_type") == "analysis_completed"
            or item.get("type") == "analysis_completed"
        )
        and item.get("global_person_id") is None
        for item in retained
    )


class ObservationAdapter:
    def __init__(self):
        self.tracks = set()

    def metrics(self, payload):
        observed = payload.get("observed_at")
        age = None
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(observed.replace("Z", "+00:00"))).total_seconds()
            fresh = 0 <= age <= 5
        except (ValueError, TypeError, AttributeError):
            fresh = False
        objects = payload.get("objects", []) if fresh else []
        session = payload.get("tracking_session_id")
        self.tracks.update((session, item["person_id"]) for item in objects)
        return {
            "current_objects": len(objects) if fresh else "—",
            "tracked_total": len(self.tracks),
            "coordinate_age_seconds": age,
        }


class VideoWorker(QThread):
    frame_ready = pyqtSignal(object)
    metrics_ready = pyqtSignal(dict)
    objects_ready = pyqtSignal(dict)
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
        self._event_lock = threading.Lock()
        self._min_after_event_id = 0
        self._last_seen_event_id = 0
        self._clear_events_requested = threading.Event()

    def clear_retained_events(self, min_after_event_id=0):
        with self._event_lock:
            self._min_after_event_id = max(
                self._min_after_event_id,
                int(min_after_event_id or 0),
                self._last_seen_event_id,
            )
            self._clear_events_requested.set()
            return self._min_after_event_id

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
        LOGGER.debug(
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
        failed = False
        while not self._stop.is_set():
            try:
                request_started = time.monotonic()
                payload = api.camera(
                    camera_id,
                    "/objects",
                    timeout=POLL_REQUEST_TIMEOUT_SECONDS,
                )
                if isinstance(payload, dict):
                    self.objects_ready.emit(payload)
                else:
                    raise ValueError("Invalid live objects response")
                received = time.monotonic()
                metrics = adapter.metrics(payload)
                metrics["coordinate_request_seconds"] = received - request_started
                metrics["coordinate_received_monotonic"] = received
                self.metrics_ready.emit(metrics)
                failed = False
            except Exception as exc:
                self.objects_ready.emit({"objects": [], "stale": True})
                self.metrics_ready.emit({"current_objects": "—", "tracked_total": len(adapter.tracks)})
                if not failed:
                    self.event_ready.emit({"type": "objects_error", "message": safe_error(exc)})
                failed = True
            self._stop.wait(OBJECT_POLL_INTERVAL_SECONDS)

    def poll_events(self):
        api, camera_id = self.source
        with self._event_lock:
            after_event_id = self._min_after_event_id
        initialized = False
        failed = False
        retained = []
        last_gpid_refresh = None
        while not self._stop.is_set():
            try:
                with self._event_lock:
                    if self._clear_events_requested.is_set():
                        retained.clear()
                        last_gpid_refresh = None
                        after_event_id = max(after_event_id, self._min_after_event_id)
                        self._clear_events_requested.clear()
                    min_after_id = self._min_after_event_id
                if not initialized:
                    page = api.events(camera_id, limit=30, order="desc")
                    current = page.get("items", [])
                    snapshot_max_id = int(page.get("snapshot_max_id") or 0)
                    with self._event_lock:
                        if self._clear_events_requested.is_set():
                            retained.clear()
                            last_gpid_refresh = None
                            self._clear_events_requested.clear()
                        elif snapshot_max_id < self._min_after_event_id:
                            self._min_after_event_id = 0
                        min_after_id = self._min_after_event_id
                        after_event_id = max(snapshot_max_id, min_after_id)
                        self._last_seen_event_id = after_event_id
                    for event in reversed(current):
                        try:
                            event_id = int(event.get("id") or 0)
                        except (TypeError, ValueError):
                            event_id = 0
                        if min_after_id and event_id <= min_after_id:
                            continue
                        item = _upsert_retained_event(retained, legacy_event(event))
                        self.event_ready.emit(dict(item))
                    if _has_unresolved_analysis_event(retained):
                        last_gpid_refresh = time.monotonic()
                    initialized = True
                else:
                    while not self._stop.is_set():
                        page = api.events(camera_id, after_event_id=after_event_id)
                        if (
                            "snapshot_max_id" in page
                            and int(page.get("snapshot_max_id") or 0) < after_event_id
                        ):
                            with self._event_lock:
                                self._min_after_event_id = 0
                                self._last_seen_event_id = int(page.get("snapshot_max_id") or 0)
                                self._clear_events_requested.clear()
                            retained.clear()
                            last_gpid_refresh = None
                            after_event_id = 0
                            page = api.events(camera_id, after_event_id=0)
                        current = page.get("items", [])
                        with self._event_lock:
                            if self._clear_events_requested.is_set():
                                retained.clear()
                                last_gpid_refresh = None
                                after_event_id = max(after_event_id, self._min_after_event_id)
                                self._clear_events_requested.clear()
                            min_after_id = self._min_after_event_id
                        for event in current:
                            try:
                                event_id = int(event.get("id") or 0)
                            except (TypeError, ValueError):
                                event_id = 0
                            with self._event_lock:
                                if event_id > self._last_seen_event_id:
                                    self._last_seen_event_id = event_id
                            if min_after_id and event_id <= min_after_id:
                                continue
                            item = _upsert_retained_event(retained, legacy_event(event))
                            self.event_ready.emit(dict(item))
                        if current:
                            after_event_id = max(after_event_id, max(int(event["id"]) for event in current))
                            with self._event_lock:
                                if after_event_id > self._last_seen_event_id:
                                    self._last_seen_event_id = after_event_id
                        if not page.get("has_more") or not current:
                            break
                    if _has_unresolved_analysis_event(retained):
                        now = time.monotonic()
                        if last_gpid_refresh is None:
                            last_gpid_refresh = now
                        elif (
                            not self._stop.is_set()
                            and now - last_gpid_refresh >= NULL_GPID_REFRESH_INTERVAL_SECONDS
                        ):
                            last_gpid_refresh = now
                            refresh_page = api.events(camera_id, limit=30, order="desc")
                            with self._event_lock:
                                if self._clear_events_requested.is_set():
                                    retained.clear()
                                    last_gpid_refresh = None
                                    after_event_id = max(after_event_id, self._min_after_event_id)
                                    self._clear_events_requested.clear()
                                min_after_id = self._min_after_event_id
                            retained_ids = {
                                str(item.get("id"))
                                for item in retained
                                if item.get("id") is not None
                            }
                            for event in reversed(refresh_page.get("items", [])):
                                event_id = event.get("id")
                                if event_id is None or str(event_id) not in retained_ids:
                                    continue
                                try:
                                    if min_after_id and int(event_id) <= min_after_id:
                                        continue
                                except (TypeError, ValueError):
                                    pass
                                updated, changed = _refresh_retained_event_ids(
                                    retained, legacy_event(event)
                                )
                                if updated is not None and changed:
                                    self.event_ready.emit(dict(updated))
                            if not _has_unresolved_analysis_event(retained):
                                last_gpid_refresh = None
                    else:
                        last_gpid_refresh = None
                failed = False
            except Exception as exc:
                if not failed:
                    self.event_ready.emit({"type": "event_error", "message": safe_error(exc)})
                failed = True
            self._stop.wait(EVENT_POLL_INTERVAL_SECONDS)

    def run(self):
        api, camera_id = self.source
        poller = threading.Thread(target=self.poll, daemon=True)
        event_poller = threading.Thread(target=self.poll_events, daemon=True)
        poller.start()
        event_poller.start()
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
            poller.join(timeout=POLL_REQUEST_TIMEOUT_SECONDS + 1)
            event_poller.join(timeout=POLL_REQUEST_TIMEOUT_SECONDS + 1)

    def _decode_loop(self, frames):
        api, camera_id = self.source
        try:
            import av
        except Exception as exc:
            self.event_ready.emit({"type": "error", "message": safe_error(exc)})
            return
        offline = False
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
                if not offline:
                    self.loading_ready.emit("서버 영상 연결 중...")
                with _open_live_stream(
                    api, camera_id, av, stop_event=self._stop
                ) as stream:
                    offline = False
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
            except CameraOfflineError:
                if not offline and not self._stop.is_set():
                    self.event_ready.emit({
                        "type": "network_failure",
                        "message": "카메라 송출이 중지되었습니다. 송출을 시작하면 자동으로 연결됩니다.",
                    })
                offline = True
                self._stop.wait(3)
            except Exception as exc:
                if not self._stop.is_set():
                    self.event_ready.emit({
                        "type": "network_failure",
                        "message": f"영상 연결 오류: {safe_error(exc)}\n3초 후 재접속합니다.",
                    })
                self._stop.wait(3)
        self._log_stats(frames.qsize(), force=True)
