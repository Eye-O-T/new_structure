# 카메라 한 대의 RTSP 영상 수신 → 사람 탐지·추적 → 실시간 좌표/이벤트 전송 흐름이다.
# 사람의 전역 식별과 추가 속성 분석은 여기서 기다리지 않고 별도 작업자가 수행한다.
from __future__ import annotations

import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from ai_cctv_core.identifiers import validate_camera_id
from ai_cctv_core.time import format_utc, utc_now
from ai_cctv_core.processing.plugins import load_factory

from ..processors.detection.contracts import DetectionFrame, DetectionResult

from .data_client import DataClient
from .event_state import TrackState
from .settings import Settings
from .objects import clip_detections, save_observation, write_jpeg_atomic
from .live_publisher import LivePublisher
from .event_publisher import EventPublisher

LOGGER = logging.getLogger("ai_cctv.preprocessing")


# 한 카메라의 연결 상태와 모델 준비 여부를 분리해 관리 API에 보고한다.
@dataclass
class WorkerStatus:
    camera_id: str
    state: str = "starting"
    model_ready: bool = False
    last_error: str | None = None
    last_frame_at: str | None = None
    event_delivery_error: str | None = None
    event_persistence_failures: int = 0
    event_backpressure: bool = False
    event_shutdown_losses: int = 0
    observation_error: str | None = None
    observation_persistence_failures: int = 0


# 카메라마다 독립 스레드·추적 세션을 두고 Data에는 상태와 관측 결과만 전달한다.
class CameraWorker(threading.Thread):
    def __init__(
        self,
        camera: dict[str, Any],
        settings: Settings,
        data_client: DataClient,
        tracker_factory: Callable[[Path, float, str], Any] | None = None,
        event_publisher: EventPublisher | None = None,
    ):
        camera_id = validate_camera_id(str(camera["camera_id"]))
        super().__init__(name=f"camera-{camera_id}", daemon=True)
        self.camera_id = camera_id
        self.stream_path = str(camera.get("stream_path") or camera_id)
        settings.rtsp_source_url(self.stream_path)
        self.settings = settings
        self.data_client = data_client
        self.tracker_factory = tracker_factory
        self.stop_event = threading.Event()
        self.status = WorkerStatus(camera_id=camera_id)
        self._failure_reported = False
        self.tracking_session_id = uuid.uuid4().hex
        self._publisher = None
        self._events = event_publisher
        self._next_model_attempt = 0.0
        self._pending_status: str | None = None
        self._last_status_attempt = 0.0
        self._created_monotonic = time.monotonic()
        self._last_frame_monotonic: float | None = None

    def stop(self) -> None:
        self.stop_event.set()

    def status_snapshot(self) -> dict[str, Any]:
        # 처리 스레드가 모델이나 저장소에서 멈춰도 API 스레드가 프레임 노후화를 계산한다.
        result = dict(vars(self.status))
        last_frame = self._last_frame_monotonic
        age = time.monotonic() - (
            self._created_monotonic if last_frame is None else last_frame
        )
        result["frame_age_seconds"] = None if last_frame is None else max(0, age)
        result["frame_stale"] = age >= max(
            30.0, self.settings.capture_timeout_seconds * 2
        )
        return result

    # 이벤트의 최상위 필드와 부가 메타데이터를 나누어 Data 입력 계약에 맞게 전송한다.
    def _event(
        self, event_type: str, *, occurred_at: datetime | None = None, **metadata: Any
    ) -> bool:
        # person_id는 현재 카메라·세션에서의 추적 번호다. 아직 카메라 간 동일 인물
        # 판정은 끝나지 않았으므로 global_person_id는 비워 Data에 전달한다.
        event_time = format_utc(occurred_at or utc_now())
        person_id = metadata.pop("person_id", None)
        snapshot_path = metadata.pop("snapshot_path", None)
        observation = metadata.pop("object_observation", None)
        if person_id is not None:
            metadata["tracking_session_id"] = self.tracking_session_id
        payload = {
            "camera_id": self.camera_id,
            "event_type": event_type,
            "occurred_at": event_time,
            "person_id": person_id,
            "global_person_id": None,
            "object_observation": observation,
            "confidence": metadata.pop("confidence", None),
            "snapshot_path": snapshot_path,
            "metadata": metadata,
        }
        if self._events is not None:
            payload["source_event_id"] = uuid.uuid4().hex
        delay = 0.5
        while True:
            try:
                if self._events is None:
                    self.data_client.create_event(payload)
                else:
                    self._events.submit(payload)
                self.status.event_delivery_error = None
                self.status.event_backpressure = False
                if self.status.last_error and self.status.last_error.startswith(
                    "event delivery failed:"
                ):
                    self.status.last_error = None
                return True
            except Exception as exc:
                if not self.status.event_backpressure:
                    LOGGER.warning(
                        "event persistence failed; detection waits for outbox capacity",
                        extra={
                            "camera_id": self.camera_id,
                            "error_code": "EVENT_STORAGE",
                        },
                    )
                self.status.last_error = f"event delivery failed: {type(exc).__name__}"
                self.status.event_delivery_error = type(exc).__name__
                self.status.event_persistence_failures += 1
                if self._events is None:
                    return False
                # 한 카메라의 현재 이벤트만 보관하고 추가 추론·스냅샷 생성을 멈춘다.
                # 저장소가 복구되면 같은 ID와 스냅샷으로 재시도하므로 중복 생성을 피한다.
                self.status.event_backpressure = True
                if self.stop_event.wait(delay):
                    self.status.event_shutdown_losses += 1
                    LOGGER.error(
                        "worker stopped with an unpersisted event; snapshots are retained",
                        extra={
                            "camera_id": self.camera_id,
                            "error_code": "EVENT_SHUTDOWN_LOSS",
                        },
                    )
                    return False
                delay = min(delay * 2, 5)

    # 로컬 상태를 먼저 갱신하고 중앙 보고 실패는 영상 처리를 중단시키지 않는다.
    def _status(self, value: str) -> None:
        self.status.state = value
        self._pending_status = value
        self._flush_status()

    def _flush_status(self) -> None:
        if self._pending_status is None:
            return
        self._last_status_attempt = time.monotonic()
        try:
            self.data_client.set_camera_status(self.camera_id, self._pending_status)
            self._pending_status = None
        except Exception:
            LOGGER.warning(
                "camera status delivery failed", extra={"camera_id": self.camera_id}
            )

    def _inference_stream_lost(self, reason: str) -> None:
        """탐지 소비자의 연결 장애를 한 번 보고한다."""

        # 녹화 복구 구간은 Edge 연결로 정하므로 여기서는 복구 작업을 변경하지 않는다.
        if self._failure_reported:
            return
        self._status("offline")
        self._event("inference_stream_lost", reason=reason)
        self._failure_reported = True

    # 이전에 보고한 탐지 연결 장애가 있을 때만 복구 이벤트를 한 번 보낸다.
    def _inference_stream_restored(self) -> None:
        if not self._failure_reported:
            return
        self._event("inference_stream_restored", reason="rtsp_stream_available")
        self._failure_reported = False

    def _snapshot(self, frame: Any, person_id: str) -> str | None:
        # 이벤트 화면용 원본 이미지를 저장하고 컨테이너 내부 절대 경로 대신
        # snapshots 공유 저장소 기준 상대 경로를 전달한다.
        try:
            now = utc_now()
            relative = (
                Path(self.camera_id)
                / now.strftime("%Y/%m/%d")
                / (f"{now.strftime('%Y%m%dT%H%M%S.%fZ')}_{uuid.uuid4().hex}.jpg")
            )
            write_jpeg_atomic(self.settings.snapshots_root, relative, frame)
            return relative.as_posix()
        except Exception:
            LOGGER.warning("snapshot write failed", extra={"camera_id": self.camera_id})
        return None

    def _observation(self, frame: Any, detection: dict) -> dict | None:
        # crop 없이 등장 이벤트만 저장하면 식별·분석 작업이 생성되지 않으므로 성공까지 기다린다.
        # 원본 프레임 한 장을 재사용하며 원본 snapshot을 반복해서 만들지 않는다.
        delay = 0.5
        while True:
            try:
                observation = save_observation(
                    frame,
                    detection,
                    self.settings.snapshots_root,
                    self.camera_id,
                    self.tracking_session_id,
                )
            except Exception as exc:
                if self.status.observation_error is None:
                    LOGGER.warning(
                        "object crop persistence failed; detection waits for storage",
                        extra={
                            "camera_id": self.camera_id,
                            "error_code": "OBJECT_CROP_WRITE_FAILED",
                        },
                    )
                self.status.observation_error = type(exc).__name__
                self.status.observation_persistence_failures += 1
                self.status.last_error = (
                    f"object crop write failed: {type(exc).__name__}"
                )
                self.status.event_delivery_error = "OBJECT_CROP_WRITE_FAILED"
                self.status.event_backpressure = True
                if self.stop_event.wait(delay):
                    self.status.event_shutdown_losses += 1
                    LOGGER.error(
                        "worker stopped before object crop persisted; appearance event was not saved",
                        extra={
                            "camera_id": self.camera_id,
                            "error_code": "EVENT_SHUTDOWN_LOSS",
                        },
                    )
                    return None
                delay = min(delay * 2, 5)
                continue
            self.status.observation_error = None
            self.status.event_backpressure = False
            if self.status.event_delivery_error == "OBJECT_CROP_WRITE_FAILED":
                self.status.event_delivery_error = None
            if self.status.last_error and self.status.last_error.startswith(
                "object crop write failed:"
            ):
                self.status.last_error = None
            return observation

    # 좌표 전송 스레드의 생성·정리를 카메라 작업자의 수명에 묶는다.
    def run(self) -> None:
        self._publisher = LivePublisher(self.data_client, self.camera_id)
        try:
            self._run()
        finally:
            self.status.state = "stopped"
            self._publisher.close()

    def _prepare_tracker(self):
        # 실패한 모델을 매 프레임 다시 만들지 않고 같은 카메라 스레드에서 간격을 두고 재준비한다.
        now = time.monotonic()
        if not self.settings.inference_enabled or now < self._next_model_attempt:
            return None
        self._next_model_attempt = now + self.settings.model_retry_seconds
        try:
            factory = self.tracker_factory or load_factory(
                self.settings.detection_plugin
            )
            tracker = factory(
                self.settings.model_path, self.settings.confidence, self.settings.device
            )
            if not callable(getattr(tracker, "process", None)) or not callable(
                getattr(tracker, "reset", None)
            ):
                raise ValueError(
                    "Detection processor requires process(frame) and reset()"
                )
            tracker.reset()
        except Exception as exc:
            self._next_model_attempt = (
                time.monotonic() + self.settings.model_retry_seconds
            )
            self.status.model_ready = False
            self.status.last_error = f"model unavailable: {type(exc).__name__}"
            LOGGER.warning("model preparation failed; a later retry is scheduled")
            return None
        self.status.model_ready = True
        self.status.last_error = None
        return tracker

    # 영상 연결과 읽기에 같은 유한 제한 시간을 적용하고 모든 경로에서 캡처를 해제한다.
    def _run(self) -> None:
        import cv2

        tracker = None
        state = TrackState(self.settings.disappear_seconds)
        source = self.settings.rtsp_source_url(self.stream_path)
        timeout_ms = max(1, int(self.settings.capture_timeout_seconds * 1000))
        parameters = [
            cv2.CAP_PROP_OPEN_TIMEOUT_MSEC,
            timeout_ms,
            cv2.CAP_PROP_READ_TIMEOUT_MSEC,
            timeout_ms,
        ]
        delay = 1.0
        frame_interval = 1.0 / self.settings.analysis_fps
        last_analysis = float("-inf")
        last_publish = float("-inf")
        while not self.stop_event.is_set():
            if tracker is None:
                tracker = self._prepare_tracker()
            if self.stop_event.is_set():
                break
            capture = None
            try:
                capture = cv2.VideoCapture(source, cv2.CAP_FFMPEG, parameters)
                if not capture.isOpened():
                    self._inference_stream_lost("rtsp_open_failed")
                else:
                    # 번호를 재사용하는 추적기를 새 세션으로 분리해 이전 관측과 섞지 않는다.
                    self.tracking_session_id = uuid.uuid4().hex
                    state = TrackState(self.settings.disappear_seconds)
                    if tracker is not None:
                        try:
                            tracker.reset()
                        except Exception as exc:
                            tracker = None
                            self.status.model_ready = False
                            self.status.last_error = (
                                f"model reset failed: {type(exc).__name__}"
                            )
                            LOGGER.warning(
                                "model reset failed; a later retry is scheduled"
                            )
                            self._next_model_attempt = (
                                time.monotonic() + self.settings.model_retry_seconds
                            )
                    stream_confirmed = False
                    while not self.stop_event.is_set():
                        ok, frame = capture.read()
                        if not ok or self.stop_event.is_set():
                            break
                        now = time.monotonic()
                        observed_at = utc_now()
                        self._last_frame_monotonic = now
                        self.status.last_frame_at = format_utc(observed_at)
                        if not stream_confirmed:
                            # 실제 프레임 수신 뒤에만 영상 연결 복구를 보고한다.
                            self._inference_stream_restored()
                            self._status("online")
                            delay = 1.0
                            stream_confirmed = True
                        if (
                            now - self._last_status_attempt
                            >= self.settings.refresh_seconds
                        ):
                            self._flush_status()
                        if tracker is None:
                            tracker = self._prepare_tracker()
                            if tracker is not None:
                                # 모델 재준비도 추적 ID가 초기화되므로 새로운 관측 세션이다.
                                self.tracking_session_id = uuid.uuid4().hex
                                state = TrackState(self.settings.disappear_seconds)
                                last_analysis = last_publish = float("-inf")
                        if tracker is None or now - last_analysis < frame_interval:
                            continue
                        last_analysis = now
                        try:
                            last_publish = self._process_frame(
                                frame, tracker, state, now, last_publish, observed_at
                            )
                        except Exception as exc:
                            tracker = None
                            self.status.model_ready = False
                            self.status.last_error = (
                                f"inference unavailable: {type(exc).__name__}"
                            )
                            self._next_model_attempt = (
                                time.monotonic() + self.settings.model_retry_seconds
                            )
                            LOGGER.warning(
                                "inference failed; video monitoring and model retry continue"
                            )
                    if not self.stop_event.is_set():
                        self._inference_stream_lost("rtsp_read_failed")
            except Exception as exc:
                self.status.last_error = f"video unavailable: {type(exc).__name__}"
                self._inference_stream_lost("rtsp_capture_failed")
                LOGGER.warning("video capture failed; reconnect is scheduled")
            finally:
                if capture is not None:
                    capture.release()
            if self._pending_status and not self.stop_event.is_set():
                if (
                    time.monotonic() - self._last_status_attempt
                    >= self.settings.refresh_seconds
                ):
                    self._flush_status()
            self.stop_event.wait(delay)
            delay = min(delay * 2, 15.0)

    # 모델 실행·좌표 발행·관측 파일 생성은 같은 카메라 스레드에서 순서대로 처리한다.
    def _process_frame(
        self, frame, tracker, state, now, last_publish, observed_at=None
    ):
        height, width = frame.shape[:2]
        observed_at = observed_at or utc_now()
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
            # 모바일 박스용 최신 좌표는 최대 초당 2번 보낸다. 영상 전송과
            # 별개이므로 모바일에서 보이는 영상 프레임과 정확히 동기화되지는 않는다.
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
            if self.stop_event.is_set():
                break
            snapshot_path = None
            observation = None
            if transition.event_type == "person_appeared":
                # 첫 등장 때 잘라낸 사람 이미지와 관측 정보를 이벤트에 붙인다.
                # Data는 이 정보를 저장한 뒤 식별·추가 분석 작업의 입력으로 사용한다.
                snapshot_path = self._snapshot(frame, transition.person_id)
                observation = self._observation(frame, by_person[transition.person_id])
                if observation is None:
                    break
            if not self._event(
                transition.event_type,
                occurred_at=observed_at,
                person_id=transition.person_id,
                confidence=transition.confidence,
                snapshot_path=snapshot_path,
                object_observation=observation,
            ):
                break
        return last_publish
