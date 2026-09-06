from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ai_cctv_core.identifiers import validate_camera_id
from ai_cctv_core.time import format_utc, utc_now
from ai_cctv_core.processing.plugins import load_factory

from ..processors.detection.contracts import DetectionFrame, DetectionResult

from .data_client import DataClient
from .event_state import TrackState
from .settings import Settings
from .objects import clip_detections, save_observation
from .live_publisher import LivePublisher

LOGGER = logging.getLogger("ai_cctv.preprocessing")


@dataclass
class WorkerStatus:
    camera_id: str
    state: str = "starting"
    model_ready: bool = False
    last_error: str | None = None
    last_frame_at: str | None = None


class CameraWorker(threading.Thread):
    def __init__(
        self,
        camera: dict[str, Any],
        settings: Settings,
        data_client: DataClient,
        tracker_factory: Callable[[Path, float, str], Any] | None = None,
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
        self.tracking_session_id = uuid.uuid4().hex
        self._publisher = None

    def stop(self) -> None:
        self.stop_event.set()

    def _event(self, event_type: str, **metadata: Any) -> None:
        occurred_at = format_utc(utc_now())
        person_id = metadata.pop("person_id", None)
        snapshot_path = metadata.pop("snapshot_path", None)
        observation = metadata.pop("object_observation", None)
        if person_id is not None:
            metadata["tracking_session_id"] = self.tracking_session_id
        payload = {
            "camera_id": self.camera_id,
            "event_type": event_type,
            "occurred_at": occurred_at,
            "person_id": person_id,
            "global_person_id": None,
            "object_observation": observation,
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

    def _snapshot(self, frame: Any, person_id: str) -> str | None:
        try:
            import cv2

            now = utc_now()
            relative = (
                Path(self.camera_id)
                / now.strftime("%Y/%m/%d")
                / (f"{now.strftime('%Y%m%dT%H%M%S.%fZ')}_{uuid.uuid4().hex}.jpg")
            )
            target = self.settings.snapshots_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            if cv2.imwrite(str(target), frame):
                return relative.as_posix()
        except Exception:
            LOGGER.warning("snapshot write failed", extra={"camera_id": self.camera_id})
        return None

    def run(self) -> None:
        self._publisher = LivePublisher(self.data_client, self.camera_id)
        try:
            self._run()
        finally:
            self._publisher.close()

    def _run(self) -> None:
        import cv2

        tracker = None
        if self.settings.inference_enabled:
            try:
                factory = self.tracker_factory or load_factory(
                    self.settings.detection_plugin
                )
                tracker = factory(
                    self.settings.model_path,
                    self.settings.confidence,
                    self.settings.device,
                )
                if not callable(getattr(tracker, "process", None)) or not callable(
                    getattr(tracker, "reset", None)
                ):
                    raise ValueError(
                        "Detection processor requires process(frame) and reset()"
                    )
                self.status.model_ready = True
            except Exception as exc:
                tracker = None
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
        last_publish = 0.0

        while not self.stop_event.is_set():
            capture = cv2.VideoCapture(source, cv2.CAP_FFMPEG)
            if not capture.isOpened():
                self._inference_stream_lost("rtsp_open_failed")
                capture.release()
                self.stop_event.wait(delay)
                delay = min(delay * 2, 15.0)
                continue

            stream_confirmed = False
            self.tracking_session_id = uuid.uuid4().hex
            state = TrackState(self.settings.disappear_seconds)
            if tracker is not None and hasattr(tracker, "reset"):
                tracker.reset()
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
                    height, width = frame.shape[:2]
                    observed_at = utc_now()
                    result = DetectionResult.model_validate(
                        tracker.process(
                            DetectionFrame(
                                camera_id=self.camera_id,
                                tracking_session_id=self.tracking_session_id,
                                observed_at=observed_at,
                                image=frame,
                            )
                        )
                    )
                    detections = clip_detections(
                        [item.model_dump() for item in result.objects], width, height
                    )
                    if self._publisher is not None and now - last_publish >= 0.5:
                        self._publisher.submit(
                            {
                                "tracking_session_id": self.tracking_session_id,
                                "observed_at": format_utc(observed_at),
                                "frame_width": width,
                                "frame_height": height,
                                "objects": detections,
                            }
                        )
                        last_publish = now
                    by_person = {d["person_id"]: d for d in detections}
                    for transition in state.update(detections, now):
                        snapshot_path = None
                        observation = None
                        if transition.event_type == "person_appeared":
                            snapshot_path = self._snapshot(frame, transition.person_id)
                            try:
                                observation = save_observation(
                                    frame,
                                    by_person[transition.person_id],
                                    self.settings.snapshots_root,
                                    self.camera_id,
                                    self.tracking_session_id,
                                )
                            except Exception:
                                LOGGER.warning(
                                    "object crop write failed",
                                    extra={"camera_id": self.camera_id},
                                )
                        self._event(
                            transition.event_type,
                            person_id=transition.person_id,
                            confidence=transition.confidence,
                            snapshot_path=snapshot_path,
                            object_observation=observation,
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
