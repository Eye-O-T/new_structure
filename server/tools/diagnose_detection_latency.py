"""Camera-free timing probe for the deployed detector and its process boundary."""
from datetime import datetime, timezone
from pathlib import Path
import time
import sys

import numpy as np

from server.services.preprocessing.processors.detection.contracts import DetectionFrame
from server.services.preprocessing.processors.detection.isolation import IsolatedDetector
from server.services.preprocessing.processors.detection.yolo import YoloTracker


def main():
    image = np.zeros((720, 1280, 3), dtype=np.uint8)
    if len(sys.argv) > 1:
        import cv2
        image = cv2.imread(sys.argv[1])
        if image is None:
            raise ValueError("Diagnostic input image could not be read")
    frame = DetectionFrame(
        camera_id="diagnostic", tracking_session_id="0" * 32,
        observed_at=datetime.now(timezone.utc),
        image=image,
    )
    started = time.monotonic()
    detector = None
    try:
        detector = IsolatedDetector(
            "server.services.preprocessing.processors.detection.yolo:YoloTracker",
            Path("/models/yolo11n.pt"), 0.25, "cuda:0",
            timeout_seconds=10, startup_timeout_seconds=30,
        )
        print("isolated_startup_seconds", round(time.monotonic() - started, 3), flush=True)
        for index in range(3):
            started = time.monotonic()
            result = detector.process(frame)
            print("isolated_frame", index, "seconds", round(time.monotonic() - started, 3),
                  "objects", len(result.objects), flush=True)
    except Exception as exc:
        print("isolated_error", type(exc).__name__, str(exc),
              "seconds", round(time.monotonic() - started, 3), flush=True)
    finally:
        if detector is not None:
            detector.close()
    started = time.monotonic()
    detector = YoloTracker(Path("/models/yolo11n.pt"), 0.25, "cuda:0")
    print("direct_startup_seconds", round(time.monotonic() - started, 3), flush=True)
    for index in range(3):
        started = time.monotonic()
        result = detector.process(frame)
        print("direct_frame", index, "seconds", round(time.monotonic() - started, 3),
              "objects", len(result.objects), flush=True)


if __name__ == "__main__":
    main()
