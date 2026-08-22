import cv2
import numpy as np
import mediapipe as mp
from pathlib import Path
from mediapipe.tasks import python
from mediapipe.tasks.python import vision
from ultralytics import YOLO
from insightface.app import FaceAnalysis

RTSP_URL = "rtsp://192.168.10.2:8554/stream"
VIDEO_SOURCE = 0

YOLO_MODEL_PATH = "yolo26s.pt"
FACE_TASK_MODEL_PATH = "blaze_face_short_range.tflite"
KNOWN_FACE_DIR = "known_faces"

TARGET_CLASSES = ["person", "apple"]
YOLO_CONF_TH = 0.5
FACE_SIM_THRESHOLD = 0.20

yolo_model = YOLO(YOLO_MODEL_PATH)

face_app = FaceAnalysis(name="buffalo_l", providers=["CPUExecutionProvider"])
face_app.prepare(ctx_id=-1, det_size=(640, 640))


def normalize_embedding(embedding: np.ndarray) -> np.ndarray:
    emb = embedding.astype(np.float32)
    norm = np.linalg.norm(emb) + 1e-8
    return emb / norm


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b))


def get_largest_face(faces):
    if not faces:
        return None
    return max(
        faces,
        key=lambda f: (f.bbox[2] - f.bbox[0]) * (f.bbox[3] - f.bbox[1])
    )


def expand_box(x1, y1, x2, y2, img_w, img_h, scale=0.4):
    w = x2 - x1
    h = y2 - y1
    dx = int(w * scale)
    dy = int(h * scale)

    nx1 = max(0, x1 - dx)
    ny1 = max(0, y1 - dy)
    nx2 = min(img_w, x2 + dx)
    ny2 = min(img_h, y2 + dy)
    return nx1, ny1, nx2, ny2


def load_known_faces(face_dir: str):
    face_db = {}
    base = Path(face_dir)

    if not base.exists():
        print(f"[경고] 등록 얼굴 폴더가 없습니다: {face_dir}")
        return face_db

    for person_dir in base.iterdir():
        if not person_dir.is_dir():
            continue

        person_name = person_dir.name
        embeddings = []

        for img_path in person_dir.iterdir():
            if img_path.suffix.lower() not in [".jpg", ".jpeg", ".png", ".bmp"]:
                continue

            img = cv2.imread(str(img_path))
            if img is None:
                continue

            faces = face_app.get(img)
            if not faces:
                print(f"[등록 실패] {person_name}: 얼굴 검출 안됨 - {img_path.name}")
                continue

            face = get_largest_face(faces)
            emb = normalize_embedding(face.embedding)
            embeddings.append(emb)

        if embeddings:
            face_db[person_name] = embeddings
            print(f"[등록 완료] {person_name}: {len(embeddings)}장")
        else:
            print(f"[등록 실패] {person_name}: 사용 가능한 얼굴 이미지 없음")

    return face_db


def recognize_face(face_crop: np.ndarray, face_db: dict, threshold=0.20):
    faces = face_app.get(face_crop)
    if not faces:
        return "Unknown", -1.0

    face = get_largest_face(faces)
    emb = normalize_embedding(face.embedding)

    best_name = "Unknown"
    best_score = -1.0

    for person_name, emb_list in face_db.items():
        scores = [cosine_similarity(emb, ref_emb) for ref_emb in emb_list]
        score = max(scores)

        if score > best_score:
            best_score = score
            best_name = person_name

    if best_score < threshold:
        return "Unknown", best_score

    return best_name, best_score


def detect_face_with_tasks(face_detector, bgr_roi: np.ndarray):
    if bgr_roi is None or bgr_roi.size == 0:
        return None

    rgb_roi = cv2.cvtColor(bgr_roi, cv2.COLOR_BGR2RGB)
    mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb_roi)
    result = face_detector.detect(mp_image)

    if not result.detections:
        return None

    h, w, _ = bgr_roi.shape
    best_box = None
    best_area = -1

    for detection in result.detections:
        bbox = detection.bounding_box
        x1 = int(bbox.origin_x)
        y1 = int(bbox.origin_y)
        x2 = int(bbox.origin_x + bbox.width)
        y2 = int(bbox.origin_y + bbox.height)

        x1 = max(0, x1)
        y1 = max(0, y1)
        x2 = min(w, x2)
        y2 = min(h, y2)

        area = max(0, x2 - x1) * max(0, y2 - y1)
        if area > best_area:
            best_area = area
            best_box = (x1, y1, x2, y2)

    return best_box


face_db = load_known_faces(KNOWN_FACE_DIR)

if not face_db:
    print("[경고] 등록 얼굴 DB가 비어 있습니다. Unknown만 표시될 수 있습니다.")

base_options = python.BaseOptions(model_asset_path=FACE_TASK_MODEL_PATH)

options = vision.FaceDetectorOptions(
    base_options=base_options,
    running_mode=vision.RunningMode.IMAGE,
    min_detection_confidence=0.5,
    min_suppression_threshold=0.3,
)

cap = cv2.VideoCapture(VIDEO_SOURCE)

if not cap.isOpened():
    print("영상 연결 실패")
    exit()

print("영상 연결 성공")

with vision.FaceDetector.create_from_options(options) as face_detector:
    while True:
        ret, frame = cap.read()

        if not ret:
            print("프레임 수신 실패")
            continue

        results = yolo_model(frame, verbose=False)
        result = results[0]

        for box in result.boxes:
            cls_id = int(box.cls[0])
            conf = float(box.conf[0])
            class_name = yolo_model.names[cls_id]

            if class_name not in TARGET_CLASSES:
                continue
            if conf < YOLO_CONF_TH:
                continue

            x1, y1, x2, y2 = map(int, box.xyxy[0])

            x1 = max(0, x1)
            y1 = max(0, y1)
            x2 = min(frame.shape[1], x2)
            y2 = min(frame.shape[0], y2)

            box_color = (0, 255, 0)
            label = f"{class_name} {conf:.2f}"

            if class_name == "person":
                person_roi = frame[y1:y2, x1:x2]
                face_box = detect_face_with_tasks(face_detector, person_roi)

                if face_box is not None:
                    fx1, fy1, fx2, fy2 = face_box

                    fx1_global = x1 + fx1
                    fy1_global = y1 + fy1
                    fx2_global = x1 + fx2
                    fy2_global = y1 + fy2

                    ex1, ey1, ex2, ey2 = expand_box(
                        fx1_global, fy1_global, fx2_global, fy2_global,
                        frame.shape[1], frame.shape[0],
                        scale=0.4
                    )

                    face_crop = frame[ey1:ey2, ex1:ex2]

                    if face_crop.size > 0:
                        person_name, sim = recognize_face(
                            face_crop,
                            face_db,
                            threshold=FACE_SIM_THRESHOLD
                        )

                        if person_name != "Unknown":
                            label = f"{person_name} / person {conf:.2f} / sim {sim:.2f}"
                            box_color = (255, 0, 0)
                        else:
                            label = f"Unknown / person {conf:.2f} / sim {sim:.2f}"
                            box_color = (0, 165, 255)

                        cv2.rectangle(
                            frame,
                            (fx1_global, fy1_global),
                            (fx2_global, fy2_global),
                            (255, 255, 0),
                            2
                        )
                    else:
                        label = f"person {conf:.2f} / face crop fail"
                else:
                    label = f"person {conf:.2f} / face not found"

            cv2.rectangle(frame, (x1, y1), (x2, y2), box_color, 2)
            cv2.putText(
                frame,
                label,
                (x1, max(20, y1 - 10)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.55,
                box_color,
                2
            )

        cv2.imshow("YOLO + MediaPipe Tasks(IMAGE) + InsightFace", frame)

        if cv2.waitKey(1) & 0xFF == ord("q"):
            break

cap.release()
cv2.destroyAllWindows()