import os
from pathlib import Path

import cv2
import numpy as np
from insightface.app import FaceAnalysis


class FaceIdentifier:
    def __init__(
        self,
        known_face_dir="known_faces",
        face_sim_threshold=0.20,
        save_debug_crops=True,
        debug_crop_dir="outputs/face_crops",
        use_cache=True,
        face_region_top_ratio=0.40,
        face_region_side_margin_ratio=0.15,
    ):
        self.known_face_dir = known_face_dir
        self.face_sim_threshold = face_sim_threshold
        self.save_debug_crops = save_debug_crops
        self.debug_crop_dir = debug_crop_dir
        self.use_cache = use_cache
        self.face_region_top_ratio = face_region_top_ratio
        self.face_region_side_margin_ratio = face_region_side_margin_ratio

        self.face_cache = {}
        self.min_face_crop_size = 20

        if self.save_debug_crops:
            os.makedirs(self.debug_crop_dir, exist_ok=True)

        self.face_app = FaceAnalysis(
            name="buffalo_l",
            providers=["CPUExecutionProvider"],
        )
        self.face_app.prepare(ctx_id=-1, det_size=(640, 640))

        self.face_db = self._load_known_faces()

    def identify_from_path(self, person_id, image_path):
        try:
            full_body_crop = cv2.imread(str(image_path))

            if full_body_crop is None:
                raise ValueError(f"이미지를 읽을 수 없습니다: {image_path}")

            return self.identify_from_crop(person_id, full_body_crop)

        except Exception as e:
            return self._error_result(person_id, str(e))

    def identify_from_crop(self, person_id, full_body_crop):
        try:
            if self.use_cache and person_id in self.face_cache:
                return dict(self.face_cache[person_id])

            full_body_crop = self._ensure_bgr_image(full_body_crop)
            if full_body_crop is None:
                return self._no_face_result(person_id)

            face_crop = self._crop_face_region(full_body_crop)
            if face_crop is None:
                return self._no_face_result(person_id)

            face_crop_path = self._save_face_crop(person_id, face_crop)
            result = self._recognize_face(person_id, face_crop, face_crop_path)

            if self.use_cache and result["status"] == "recognized":
                self.face_cache[person_id] = dict(result)

            return result

        except Exception as e:
            return self._error_result(person_id, str(e))

    def _load_known_faces(self):
        face_db = {}
        base_dir = Path(self.known_face_dir)

        if not base_dir.exists():
            print(f"[경고] 등록 얼굴 폴더가 없습니다: {self.known_face_dir}")
            return face_db

        for person_dir in base_dir.iterdir():
            if not person_dir.is_dir():
                continue

            person_name = person_dir.name
            embeddings = []

            for image_path in person_dir.iterdir():
                if image_path.suffix.lower() not in [".jpg", ".jpeg", ".png", ".bmp"]:
                    continue

                image = cv2.imread(str(image_path))
                if image is None:
                    continue

                face = self._get_largest_face(image)
                if face is None:
                    print(f"[등록 실패] {person_name}: 얼굴 검출 안됨 - {image_path.name}")
                    continue

                embeddings.append(self._normalize_embedding(face.embedding))

            if embeddings:
                face_db[person_name] = embeddings
                print(f"[등록 완료] {person_name}: {len(embeddings)}장")
            else:
                print(f"[등록 실패] {person_name}: 사용 가능한 얼굴 이미지 없음")

        return face_db

    def _recognize_face(self, person_id, face_crop, face_crop_path):
        if not self.face_db:
            return self._no_registered_faces_result(person_id, face_crop_path)

        face = self._get_largest_face(face_crop)

        if face is None:
            return self._no_face_result(person_id, face_crop_path)

        embedding = self._normalize_embedding(face.embedding)
        best_name = "Unknown"
        best_score = -1.0

        for person_name, embeddings in self.face_db.items():
            scores = [
                self._cosine_similarity(embedding, ref_embedding)
                for ref_embedding in embeddings
            ]
            score = max(scores)

            if score > best_score:
                best_score = score
                best_name = person_name

        if best_score < self.face_sim_threshold:
            return {
                "person_id": person_id,
                "name": "Unknown",
                "score": round(float(best_score), 4),
                "status": "unknown",
                "face_crop_path": face_crop_path,
            }

        return {
            "person_id": person_id,
            "name": best_name,
            "score": round(float(best_score), 4),
            "status": "recognized",
            "face_crop_path": face_crop_path,
        }

    def _crop_face_region(self, full_body_crop):
        height, width = full_body_crop.shape[:2]

        top_height = int(height * self.face_region_top_ratio)
        side_margin = int(width * self.face_region_side_margin_ratio)

        x1 = max(0, side_margin)
        x2 = min(width, width - side_margin)
        y1 = 0
        y2 = min(height, top_height)

        crop_width = x2 - x1
        crop_height = y2 - y1

        if crop_width < self.min_face_crop_size or crop_height < self.min_face_crop_size:
            return None

        face_crop = full_body_crop[y1:y2, x1:x2]

        if face_crop.size == 0:
            return None

        return face_crop

    def _save_face_crop(self, person_id, face_crop):
        if not self.save_debug_crops:
            return None

        os.makedirs(self.debug_crop_dir, exist_ok=True)

        save_path = os.path.join(
            self.debug_crop_dir,
            f"person_{person_id}_face.jpg",
        )

        if not cv2.imwrite(save_path, face_crop):
            raise RuntimeError(f"얼굴 후보 crop 저장 실패: {save_path}")

        return save_path

    def _get_largest_face(self, image):
        faces = self.face_app.get(image)

        if not faces:
            return None

        return max(
            faces,
            key=lambda face: (
                (face.bbox[2] - face.bbox[0])
                * (face.bbox[3] - face.bbox[1])
            ),
        )

    def _ensure_bgr_image(self, image):
        if image is None or not isinstance(image, np.ndarray) or image.size == 0:
            return None

        if image.ndim == 2:
            return cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)

        if image.ndim != 3:
            raise ValueError("지원하지 않는 이미지 형식입니다.")

        if image.shape[2] == 3:
            return image

        if image.shape[2] == 4:
            return cv2.cvtColor(image, cv2.COLOR_BGRA2BGR)

        raise ValueError("지원하지 않는 이미지 채널 수입니다.")

    @staticmethod
    def _normalize_embedding(embedding):
        embedding = embedding.astype(np.float32)
        norm = np.linalg.norm(embedding) + 1e-8
        return embedding / norm

    @staticmethod
    def _cosine_similarity(a, b):
        return float(np.dot(a, b))

    @staticmethod
    def _no_face_result(person_id, face_crop_path=None):
        return {
            "person_id": person_id,
            "name": "Unknown",
            "score": -1.0,
            "status": "no_face",
            "face_crop_path": face_crop_path,
        }

    @staticmethod
    def _no_registered_faces_result(person_id, face_crop_path=None):
        return {
            "person_id": person_id,
            "name": "Unknown",
            "score": -1.0,
            "status": "no_registered_faces",
            "face_crop_path": face_crop_path,
        }

    @staticmethod
    def _error_result(person_id, error):
        return {
            "person_id": person_id,
            "name": "Unknown",
            "score": -1.0,
            "status": "error",
            "error": error,
        }
