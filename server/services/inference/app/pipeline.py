from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ai_cctv_core.identifiers import validate_camera_id
from ai_cctv_core.time import format_utc, utc_now

from .data_client import DataClient
from .event_state import TrackState
from .settings import Settings

LOGGER = logging.getLogger("ai_cctv.inference")


@dataclass
class WorkerStatus:
    camera_id: str
    state: str = "starting"
    model_ready: bool = False
    last_error: str | None = None
    last_frame_at: str | None = None


class YoloTracker:
    def __init__(self, model_path: Path, confidence: float, device: str):
        from ultralytics import YOLO

        self._model = YOLO(str(model_path))
        self._confidence = confidence
        self._device = None if device == "auto" else device

    def detect(self, frame: Any) -> list[dict[str, Any]]:
        result_set = self._model.track(
            frame,
            persist=True,
            tracker="bytetrack.yaml",
            classes=[0],
            conf=self._confidence,
            device=self._device,
            verbose=False,
        )
        if not result_set or result_set[0].boxes is None:
            return []

        detections: list[dict[str, Any]] = []
        for box in result_set[0].boxes:
            if box.id is None:
                continue
            detections.append(
                {
                    "track_id": str(int(box.id[0])),
                    "confidence": float(box.conf[0]),
                    "bbox": [int(value) for value in box.xyxy[0]],
                }
            )
        return detections


class CameraWorker(threading.Thread):
    def __init__(
        self,
        camera: dict[str, Any],
        settings: Settings,
        data_client: DataClient,
        tracker_factory: Callable[[Path, float, str], Any] = YoloTracker,
    ):
        camera_id = validate_camera_id(str(camera["camera_id"]))
        super().__init__(name=f"camera-{camera_id}", daemon=True)
        self.camera_id = camera_id
        self.stream_path = str(camera.get("stream_path") or camera_id)
        self.settings = settings
        self.data_client = data_client
        self.tracker_factory = tracker_factory
        self.stop_event = threading.Event()
        self.status = WorkerStatus(camera_id=camera_id)
        self._failure_reported = False

    def stop(self) -> None:
        self.stop_event.set()

    def _event(self, event_type: str, **metadata: Any) -> None:
        occurred_at = format_utc(utc_now())
        track_id = metadata.pop("track_id", None)
        snapshot_path = metadata.pop("snapshot_path", None)
        payload = {
            "camera_id": self.camera_id,
            "event_type": event_type,
            "occurred_at": occurred_at,
            "person_id": track_id,
            "track_id": track_id,
            "confidence": metadata.pop("confidence", None),
            "snapshot_path": snapshot_path,
            "metadata": metadata,
        }
        try:
            self.data_client.create_event(payload)
        except Exception as exc:  # event failure must not terminate video consumption
            LOGGER.warning(
                "event delivery failed",
                extra={"camera_id": self.camera_id, "error_code": "EVENT_DELIVERY"},
            )
            self.status.last_error = f"event delivery failed: {type(exc).__name__}"

    def _status(self, value: str) -> None:
        self.status.state = value
        try:
            self.data_client.set_camera_status(self.camera_id, value)
        except Exception:
            LOGGER.warning(
                "camera status delivery failed", extra={"camera_id": self.camera_id}
            )

    def _inference_stream_lost(self, reason: str) -> None:
        """Report one inference-consumer outage while retrying MediaMTX.

        Edge-to-central ingest loss is detected by the Edge publisher and is
        the authoritative trigger for segment recovery. A failure at this
        downstream consumer must not create or truncate an Edge recovery job.
        """

        if self._failure_reported:
            return
        self._status("offline")
        self._event("inference_stream_lost", reason=reason)
        self._failure_reported = True

    def _inference_stream_restored(self) -> None:
        if not self._failure_reported:
            return
        self._event("inference_stream_restored", reason="rtsp_stream_available")
        self._failure_reported = False

    def _snapshot(self, frame: Any, track_id: str) -> str | None:
        try:
            import cv2

            now = utc_now()
            relative = (
                Path(self.camera_id)
                / now.strftime("%Y/%m/%d")
                / (f"{now.strftime('%Y%m%dT%H%M%S.%fZ')}_{track_id}.jpg")
            )
            target = self.settings.snapshots_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if cv2.imwrite(str(target), frame):
                return relative.as_posix()
        except Exception:
            LOGGER.warning("snapshot write failed", extra={"camera_id": self.camera_id})
        return None

    def run(self) -> None:
        import cv2

        tracker = None
        if self.settings.inference_enabled:
            try:
                tracker = self.tracker_factory(
                    self.settings.model_path,
                    self.settings.confidence,
                    self.settings.device,
                )
                self.status.model_ready = True
            except Exception as exc:
                self.status.last_error = f"model unavailable: {type(exc).__name__}"
                LOGGER.error(
                    "model load failed; continuing in non-inference mode",
                    extra={"camera_id": self.camera_id, "error_code": "MODEL_LOAD"},
                )

        state = TrackState(self.settings.disappear_seconds)
        source = self.settings.rtsp_source_url(self.stream_path)
        delay = 1.0
        frame_interval = 1.0 / self.settings.analysis_fps
        last_analysis = 0.0

        while not self.stop_event.is_set():
            capture = cv2.VideoCapture(source, cv2.CAP_FFMPEG)
            if not capture.isOpened():
                self._inference_stream_lost("rtsp_open_failed")
                capture.release()
                self.stop_event.wait(delay)
                delay = min(delay * 2, 15.0)
                continue

            stream_confirmed = False
            while not self.stop_event.is_set():
                ok, frame = capture.read()
                if not ok:
                    break
                if not stream_confirmed:
                    # Some RTSP backends report an opened socket before media
                    # arrives.  Close the outage only after a decodable frame,
                    # otherwise automatic recovery can truncate the gap.
                    self._inference_stream_restored()
                    self._status("online")
                    delay = 1.0
                    stream_confirmed = True
                now = time.monotonic()
                self.status.last_frame_at = format_utc(utc_now())
                if tracker is None or now - last_analysis < frame_interval:
                    continue
                last_analysis = now
                try:
                    detections = tracker.detect(frame)
                    for transition in state.update(detections, now):
                        snapshot_path = None
                        if transition.event_type == "person_appeared":
                            snapshot_path = self._snapshot(frame, transition.track_id)
                        self._event(
                            transition.event_type,
                            track_id=transition.track_id,
                            confidence=transition.confidence,
                            snapshot_path=snapshot_path,
                        )
                except Exception as exc:
                    tracker = None
                    self.status.model_ready = False
                    self.status.last_error = f"inference disabled: {type(exc).__name__}"
                    LOGGER.exception(
                        "inference failed; video monitoring continues",
                        extra={"camera_id": self.camera_id},
                    )
            capture.release()
            if not self.stop_event.is_set():
                self._inference_stream_lost("rtsp_read_failed")
