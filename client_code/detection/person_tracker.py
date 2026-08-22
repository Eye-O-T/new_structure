# person_tracker.py

import math
import time


class PersonTracker:
    def __init__(
        self,
        model_path="yolo26s.pt",
        target_class="person",
        conf_threshold=0.4,
        tracker_config="bytetrack.yaml",
        reid_timeout=3.0,
        reid_iou_threshold=0.18,
        reid_center_distance_ratio=0.65,
        inference_size=640,
    ):
        from ultralytics import YOLO

        self.model = YOLO(model_path)
        self.target_class = target_class
        self.conf_threshold = conf_threshold
        self.tracker_config = tracker_config
        self.inference_size = inference_size
        self.target_class_ids = [
            class_id
            for class_id, class_name in self.model.names.items()
            if class_name == self.target_class
        ]
        self.id_mapper = StablePersonIdMapper(
            reid_timeout=reid_timeout,
            iou_threshold=reid_iou_threshold,
            center_distance_ratio=reid_center_distance_ratio,
        )

    def track(self, frame):
        """
        YOLO 추론 + 객체 추적 수행

        return:
        [
            {
                "person_id": 1,
                "bbox": (x1, y1, x2, y2),
                "conf": 0.87,
                "class_name": "person"
            }
        ]
        """

        try:
            results = self.model.track(
                frame,
                persist=True,
                tracker=self.tracker_config,
                verbose=False,
                imgsz=self.inference_size,
                conf=self.conf_threshold,
                classes=self.target_class_ids or None,
            )
        except Exception as optimized_error:
            print(
                "YOLO 최적화 추론 실패, 기본 추론으로 재시도합니다: "
                f"{optimized_error}"
            )
            results = self.model.track(
                frame,
                persist=True,
                tracker=self.tracker_config,
                verbose=False
            )

        tracked_persons = []

        if results is None or len(results) == 0:
            return tracked_persons

        result = results[0]

        if result.boxes is None:
            return tracked_persons

        for box in result.boxes:
            cls_id = int(box.cls[0])
            conf = float(box.conf[0])
            class_name = self.model.names[cls_id]

            if class_name != self.target_class:
                continue

            if conf < self.conf_threshold:
                continue

            # track_id가 없는 경우 방어 처리
            if box.id is None:
                continue

            raw_person_id = int(box.id[0])

            x1, y1, x2, y2 = map(int, box.xyxy[0])
            bbox = (x1, y1, x2, y2)
            person_id = self.id_mapper.resolve(raw_person_id, bbox)

            tracked_persons.append({
                "person_id": person_id,
                "raw_person_id": raw_person_id,
                "bbox": bbox,
                "conf": conf,
                "class_name": class_name
            })

        self.id_mapper.finish_frame()
        return tracked_persons


class StablePersonIdMapper:
    def __init__(
        self,
        reid_timeout=3.0,
        iou_threshold=0.18,
        center_distance_ratio=0.65,
    ):
        self.reid_timeout = reid_timeout
        self.iou_threshold = iou_threshold
        self.center_distance_ratio = center_distance_ratio
        self.raw_to_stable = {}
        self.stable_tracks = {}
        self.next_stable_id = 1
        self.used_stable_ids = set()

    def resolve(self, raw_id, bbox):
        now = time.time()
        self._cleanup(now)

        stable_id = self.raw_to_stable.get(raw_id)
        if stable_id in self.stable_tracks:
            self._update_track(stable_id, raw_id, bbox, now)
            return stable_id

        stable_id = self._find_matching_stable_id(bbox, now)
        if stable_id is None:
            stable_id = self.next_stable_id
            self.next_stable_id += 1

        self.raw_to_stable[raw_id] = stable_id
        self._update_track(stable_id, raw_id, bbox, now)
        return stable_id

    def finish_frame(self):
        self.used_stable_ids.clear()

    def _update_track(self, stable_id, raw_id, bbox, now):
        self.stable_tracks[stable_id] = {
            "raw_id": raw_id,
            "bbox": bbox,
            "last_seen": now,
        }
        self.used_stable_ids.add(stable_id)

    def _find_matching_stable_id(self, bbox, now):
        best_id = None
        best_score = -1.0

        for stable_id, track in self.stable_tracks.items():
            if stable_id in self.used_stable_ids:
                continue

            if now - track["last_seen"] > self.reid_timeout:
                continue

            previous_bbox = track["bbox"]
            iou = self._iou(previous_bbox, bbox)
            center_score = self._center_score(previous_bbox, bbox)

            if iou < self.iou_threshold and center_score <= 0:
                continue

            score = iou + center_score
            if score > best_score:
                best_score = score
                best_id = stable_id

        return best_id

    def _cleanup(self, now):
        expired_stable_ids = {
            stable_id
            for stable_id, track in self.stable_tracks.items()
            if now - track["last_seen"] > self.reid_timeout
        }

        for stable_id in expired_stable_ids:
            del self.stable_tracks[stable_id]

        for raw_id, stable_id in list(self.raw_to_stable.items()):
            if stable_id in expired_stable_ids:
                del self.raw_to_stable[raw_id]

    def _center_score(self, bbox_a, bbox_b):
        ax, ay = self._center(bbox_a)
        bx, by = self._center(bbox_b)
        distance = math.hypot(ax - bx, ay - by)
        allowed_distance = max(
            self._diagonal(bbox_a),
            self._diagonal(bbox_b),
        ) * self.center_distance_ratio

        if allowed_distance <= 0 or distance > allowed_distance:
            return 0

        return 1.0 - (distance / allowed_distance)

    def _center(self, bbox):
        x1, y1, x2, y2 = bbox
        return (x1 + x2) / 2, (y1 + y2) / 2

    def _diagonal(self, bbox):
        x1, y1, x2, y2 = bbox
        return math.hypot(x2 - x1, y2 - y1)

    def _iou(self, bbox_a, bbox_b):
        ax1, ay1, ax2, ay2 = bbox_a
        bx1, by1, bx2, by2 = bbox_b

        inter_x1 = max(ax1, bx1)
        inter_y1 = max(ay1, by1)
        inter_x2 = min(ax2, bx2)
        inter_y2 = min(ay2, by2)

        inter_width = max(0, inter_x2 - inter_x1)
        inter_height = max(0, inter_y2 - inter_y1)
        intersection = inter_width * inter_height

        area_a = max(0, ax2 - ax1) * max(0, ay2 - ay1)
        area_b = max(0, bx2 - bx1) * max(0, by2 - by1)
        union = area_a + area_b - intersection

        if union <= 0:
            return 0

        return intersection / union
