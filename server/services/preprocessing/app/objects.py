# 탐지 좌표를 영상 크기에 맞추고, 다른 모델이 사용할 사람 이미지와 박스 이미지를 만든다.

import uuid
import os
import re
import tempfile
from pathlib import Path

from ai_cctv_core.time import utc_now
from ai_cctv_core.identifiers import safe_storage_path, validate_camera_id


def clip_detections(detections, width, height):
    # 모델 좌표가 영상 밖으로 나가면 경계 안으로 잘라낸다. 빈 박스와 중복 ID를 제외해
    # 이미지 자르기와 모바일 좌표 변환에 유효한 사각형만 전달한다.
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


def write_jpeg_atomic(root: Path, relative: Path, frame) -> None:
    """완성된 JPEG만 공유 저장소에 공개하고 심볼릭 링크를 통한 경로 탈출도 거부한다."""
    import cv2

    target = safe_storage_path(root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded, image = cv2.imencode(".jpg", frame)
    if not encoded:
        raise OSError("Cannot encode snapshot JPEG")
    descriptor, name = tempfile.mkstemp(prefix=".snapshot-", dir=target.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(image.tobytes())
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


# 유효한 픽셀 박스를 전제로 모델용 crop과 표시용 사본을 저장해 관측 계약을 반환한다.
def save_observation(frame, detection, root: Path, camera_id, session_id):
    validate_camera_id(camera_id)
    if not isinstance(session_id, str) or not re.fullmatch(r"[a-f0-9]{32}", session_id):
        raise ValueError("tracking session must be a UUID hex string")
    import cv2

    height, width = frame.shape[:2]
    box = detection["bbox"]
    x1, y1, x2, y2 = box
    if not (0 <= x1 < x2 <= width and 0 <= y1 < y2 <= height):
        raise ValueError("object crop must fit inside the frame")
    relative = Path(camera_id) / utc_now().strftime("%Y/%m/%d") / uuid.uuid4().hex
    crop = relative.with_name(relative.name + "_crop.jpg")
    annotated = relative.with_name(relative.name + "_boxed.jpg")
    # crop은 모델 입력용 사람 영역, annotated는 사용자가 위치와 ID를 확인할 전체 이미지다.
    write_jpeg_atomic(root, crop, frame[y1:y2, x1:x2])
    marked = frame.copy()
    # 원본 프레임에 직접 그리지 않아 다른 사람의 crop이나 탐지 입력에 박스가 섞이지 않는다.
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
    try:
        write_jpeg_atomic(root, annotated, marked)
    except (OSError, cv2.error):
        boxed_path = None
    else:
        boxed_path = annotated.as_posix()
    return {
        # 공유 저장소의 상대 경로와 원본 크기를 보내므로 컨테이너별 마운트 위치에 의존하지 않는다.
        "schema_version": 1,
        "tracking_session_id": session_id,
        "object_class": "person",
        "bbox": box,
        "frame_width": width,
        "frame_height": height,
        "crop_path": crop.as_posix(),
        "annotated_snapshot_path": boxed_path,
    }
