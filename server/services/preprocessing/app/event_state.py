# 매 프레임의 탐지 결과를 사람의 등장·사라짐 이벤트로 바꾼다.
# 같은 사람이 계속 보이는 동안에는 새 등장 이벤트를 반복해서 만들지 않는다.
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TrackEvent:
    event_type: str
    person_id: str
    confidence: float | None


class TrackState:
    """프레임별 탐지 결과에서 등장·퇴장 전환을 구한다."""

    def __init__(self, disappear_seconds: float):
        self._disappear_seconds = disappear_seconds
        self._tracks: dict[str, tuple[float, float | None]] = {}

    def update(self, detections: list[dict], now_monotonic: float) -> list[TrackEvent]:
        # monotonic 시간은 PC 시계가 보정되어도 뒤로 가지 않아 경과 시간 계산에 적합하다.
        events: list[TrackEvent] = []
        seen: set[str] = set()

        for detection in detections:
            person_id = str(detection["person_id"])
            confidence = detection.get("confidence")
            seen.add(person_id)
            if person_id not in self._tracks:
                events.append(TrackEvent("person_appeared", person_id, confidence))
            self._tracks[person_id] = (now_monotonic, confidence)

        for person_id, (last_seen, confidence) in list(self._tracks.items()):
            if person_id in seen:
                continue
            if now_monotonic - last_seen >= self._disappear_seconds:
                # 잠깐 가려지거나 한 프레임에서 탐지를 놓친 것을 퇴장으로 판단하지 않도록 기다린다.
                events.append(TrackEvent("person_disappeared", person_id, confidence))
                del self._tracks[person_id]
        return events
