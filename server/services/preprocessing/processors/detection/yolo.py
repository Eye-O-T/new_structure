"""Optional baseline YOLO/ByteTrack implementation behind the detection contract."""

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
                continue
            detections.append(
                {
                    "person_id": str(int(box.id[0])),
                    "confidence": float(box.conf[0]),
                    "bbox": [int(value) for value in box.xyxy[0]],
                }
            )
        return DetectionResult(objects=detections[:100])
