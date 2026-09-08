# 카메라 한 대의 RTSP 영상 수신 → 사람 탐지·추적 → 실시간 좌표/이벤트 전송 흐름이다.
# 사람의 전역 식별과 추가 속성 분석은 여기서 기다리지 않고 별도 작업자가 수행한다.
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
        # person_id는 현재 카메라·세션에서의 추적 번호다. 아직 카메라 간 동일 인물
        # 판정은 끝나지 않았으므로 global_person_id는 비워 Data에 전달한다.
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
        except Exception as exc:  # 이벤트 전송 실패가 영상 읽기까지 중단시키지 않게 한다.
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

        # 이 장애는 MediaMTX → 탐지 소비자 구간의 장애다. Edge → 중앙 서버 구간의
        # 장애와 구별하며, 여기서 Edge 녹화 복구 작업을 생성하거나 끝내지 않는다.
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
        # 이벤트 화면용 원본 이미지를 저장하고 컨테이너 내부 절대 경로 대신
        # snapshots 공유 저장소 기준 상대 경로를 전달한다.
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

        # 탐지는 현재 프레임의 사람 위치를 찾고, 추적은 연속 프레임의 사람에 ID를 붙인다.
        # 모델 로드에 실패해도 영상 연결 상태 감시는 계속할 수 있도록 따로 처리한다.
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
                # 재접속 간격을 최대 15초까지 늘려 연결 장애 중 과도한 요청을 피한다.
                delay = min(delay * 2, 15.0)
                continue

            stream_confirmed = False
            # 재접속하면 추적기의 번호가 재사용될 수 있다. 새 세션 ID와 함께 초기화해
            # 이전 세션의 person_id를 같은 인물의 연속 관측으로 잘못 연결하지 않는다.
            self.tracking_session_id = uuid.uuid4().hex
            state = TrackState(self.settings.disappear_seconds)
            if tracker is not None and hasattr(tracker, "reset"):
                tracker.reset()
            while not self.stop_event.is_set():
                ok, frame = capture.read()
                if not ok:
                    break
                if not stream_confirmed:
                    # RTSP 연결 성공만으로 영상이 도착했다고 볼 수 없다.
                    # 실제 프레임을 읽은 뒤에만 탐지 구간의 장애가 복구되었다고 알린다.
                    self._inference_stream_restored()
                    self._status("online")
                    delay = 1.0
                    stream_confirmed = True
                now = time.monotonic()
                self.status.last_frame_at = format_utc(utc_now())
                if tracker is None or now - last_analysis < frame_interval:
                    # 모든 프레임은 계속 읽되 모델은 설정된 빈도로만 호출해 처리 부하를 줄인다.
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
                        snapshot_path = None
                        observation = None
                        if transition.event_type == "person_appeared":
                            # 첫 등장 때 잘라낸 사람 이미지와 관측 정보를 이벤트에 붙인다.
                            # Data는 이 정보를 저장한 뒤 식별·추가 분석 작업의 입력으로 사용한다.
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
