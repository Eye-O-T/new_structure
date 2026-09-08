"""Optional baseline YOLO/ByteTrack implementation behind the detection contract."""

# 기본 구현은 YOLO로 사람 위치를 찾고 ByteTrack으로 프레임 사이의 위치를 연결해 ID를 붙인다.
# 얼굴·외형으로 여러 카메라의 동일 인물을 판별하는 재식별 모델은 여기에 포함하지 않는다.

from pathlib import Path
from typing import Any

from .contracts import DetectionFrame, DetectionResult


class YoloTracker:
    def __init__(self, model_path: Path, confidence: float, device: str):
        from ultralytics import YOLO

        self._model = YOLO(str(model_path))
        self._confidence = confidence
        self._device = None if device == "auto" else device

    def reset(self):
        for tracker in getattr(self._model.predictor, "trackers", []):
            tracker.reset()

    def process(self, frame: DetectionFrame) -> DetectionResult:
        # persist=True로 직전 프레임의 추적 상태를 유지한다. classes=[0]은
        # 기본 COCO 클래스 체계의 사람만 고르므로 교체 모델도 해당 클래스 구성을 확인해야 한다.
        result_set = self._model.track(
            frame.image,
            persist=True,
            tracker="bytetrack.yaml",
            classes=[0],
            conf=self._confidence,
            device=self._device,
            verbose=False,
        )
        if not result_set or result_set[0].boxes is None:
            return DetectionResult()

        detections: list[dict[str, Any]] = []
        for box in result_set[0].boxes:
            if box.id is None:
                # 위치가 감지되어도 추적 ID가 아직 없는 결과는 인물별 이벤트에 사용하지 않는다.
                continue
            detections.append(
                {
                    # 모델의 추적 번호는 카메라·세션 범위의 person_id로만 사용한다.
                    "person_id": str(int(box.id[0])),
                    "confidence": float(box.conf[0]),
                    "bbox": [int(value) for value in box.xyxy[0]],
                }
            )
        return DetectionResult(objects=detections[:100])
