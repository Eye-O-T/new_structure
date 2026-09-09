"""짧은 관측 창의 대표 프레임을 고르며 메모리와 지연의 상한을 유지한다."""

from dataclasses import dataclass
from datetime import datetime
import math
import threading


@dataclass
class Candidate:
    person_id: str
    started: float
    occurred_at: datetime
    selected_at: datetime
    frame: object
    detection: dict
    score: tuple


def quality(frame, detection):
    import cv2
    import numpy as np

    x1, y1, x2, y2 = detection["bbox"]
    crop = frame[y1:y2, x1:x2]
    smallest = min(x2 - x1, y2 - y1)
    valid = smallest >= 16 and not np.all(
        crop.max(axis=(0, 1)) == crop.min(axis=(0, 1))
    )
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    if max(gray.shape) > 256:
        scale = 256 / max(gray.shape)
        gray = cv2.resize(
            gray,
            (
                max(1, round(gray.shape[1] * scale)),
                max(1, round(gray.shape[0] * scale)),
            ),
        )
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    area = (x2 - x1) * (y2 - y1)
    # 확률이 아닌 선택 순위다. 정보 있는 크롭, 선명도와 충분한 영역, 크기 순으로 비교한다.
    return (int(valid), min(sharpness, 500) * min(area / 32768, 1), math.log1p(area))


class ObservationBuffer:
    def __init__(self, window_seconds, max_bytes):
        self.window_seconds = window_seconds
        self.max_bytes = max_bytes
        self.pending: dict[str, Candidate] = {}
        self._lock = threading.RLock()

    def update(self, frame, detections, transitions, now, observed_at):
        with self._lock:
            return self._update(frame, detections, transitions, now, observed_at)

    def _update(self, frame, detections, transitions, now, observed_at):
        appeared = {
            item.person_id
            for item in transitions
            if item.event_type == "person_appeared"
        }
        departed = {
            item.person_id
            for item in transitions
            if item.event_type == "person_disappeared"
        }
        retained_frame = None
        for detection in detections:
            person = detection["person_id"]
            previous = self.pending.get(person)
            if previous is None and person not in appeared:
                continue
            score = quality(frame, detection)
            if previous is not None and score <= previous.score:
                continue
            if retained_frame is None:
                retained_frame = frame.copy()
            self.pending[person] = Candidate(
                person,
                now if previous is None else previous.started,
                observed_at if previous is None else previous.occurred_at,
                observed_at,
                retained_frame,
                dict(detection),
                score,
            )
        due = []
        for person, candidate in list(self.pending.items()):
            if now - candidate.started >= self.window_seconds or person in departed:
                due.append(self.pending.pop(person))
        # 한 프레임은 여러 사람의 후보가 공유한다. 메모리 초과 시 오래된 후보부터 즉시 확정한다.
        while self.bytes_used > self.max_bytes:
            person = min(self.pending, key=lambda item: self.pending[item].started)
            due.append(self.pending.pop(person))
        return due

    @property
    def bytes_used(self):
        with self._lock:
            return sum(
                {
                    id(item.frame): item.frame.nbytes for item in self.pending.values()
                }.values()
            )

    def drain(self):
        with self._lock:
            result = list(self.pending.values())
            self.pending.clear()
            return result
