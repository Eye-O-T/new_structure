"""Frame-space boxes and per-appearance crops, independent of identity models."""

import uuid
from pathlib import Path

from ai_cctv_core.time import utc_now


def clip_detections(detections, width, height):
    result = []
    seen = set()
    for detection in detections:
        person_id = str(detection["person_id"])
        if person_id in seen:
            continue
        x1, y1, x2, y2 = [int(v) for v in detection["bbox"]]
        box = [
            max(0, min(width, x1)),
            max(0, min(height, y1)),
            max(0, min(width, x2)),
            max(0, min(height, y2)),
        ]
        if box[0] >= box[2] or box[1] >= box[3]:
            continue
        seen.add(person_id)
        result.append({**detection, "person_id": person_id, "bbox": box})
    return result[:100]


def save_observation(frame, detection, root: Path, camera_id, session_id):
    import cv2

    height, width = frame.shape[:2]
    box = detection["bbox"]
    x1, y1, x2, y2 = box
    relative = Path(camera_id) / utc_now().strftime("%Y/%m/%d") / uuid.uuid4().hex
    crop = relative.with_name(relative.name + "_crop.jpg")
    annotated = relative.with_name(relative.name + "_boxed.jpg")
    (root / crop).parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(root / crop), frame[y1:y2, x1:x2]):
        raise OSError("Cannot write object crop")
    marked = frame.copy()
    cv2.rectangle(marked, (x1, y1), (x2, y2), (0, 255, 0), 2)
    cv2.putText(
        marked,
        f"P:{detection['person_id']}",
        (x1, max(20, y1)),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.6,
        (0, 255, 0),
        2,
    )
    boxed_path = (
        annotated.as_posix() if cv2.imwrite(str(root / annotated), marked) else None
    )
    return {
        "schema_version": 1,
        "tracking_session_id": session_id,
        "object_class": "person",
        "bbox": box,
        "frame_width": width,
        "frame_height": height,
        "crop_path": crop.as_posix(),
        "annotated_snapshot_path": boxed_path,
    }
