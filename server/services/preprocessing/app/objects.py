# 탐지 좌표를 영상 크기에 맞추고, 다른 모델이 사용할 사람 이미지와 박스 이미지를 만든다.

import uuid
from pathlib import Path

from ai_cctv_core.time import utc_now


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


def save_observation(frame, detection, root: Path, camera_id, session_id):
    import cv2

    height, width = frame.shape[:2]
    box = detection["bbox"]
    x1, y1, x2, y2 = box
    relative = Path(camera_id) / utc_now().strftime("%Y/%m/%d") / uuid.uuid4().hex
    crop = relative.with_name(relative.name + "_crop.jpg")
    annotated = relative.with_name(relative.name + "_boxed.jpg")
    # crop은 모델 입력용 사람 영역, annotated는 사용자가 위치와 ID를 확인할 전체 이미지다.
    (root / crop).parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(root / crop), frame[y1:y2, x1:x2]):
        raise OSError("Cannot write object crop")
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
    boxed_path = (
        annotated.as_posix() if cv2.imwrite(str(root / annotated), marked) else None
    )
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
