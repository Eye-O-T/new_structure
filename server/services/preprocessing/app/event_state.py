from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class TrackEvent:
    event_type: str
    person_id: str
    confidence: float | None


class TrackState:
    """Turns per-frame detections into appearance/disappearance transitions."""

    def __init__(self, disappear_seconds: float):
        self._disappear_seconds = disappear_seconds
        self._tracks: dict[str, tuple[float, float | None]] = {}

    def update(self, detections: list[dict], now_monotonic: float) -> list[TrackEvent]:
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
                events.append(TrackEvent("person_disappeared", person_id, confidence))
                del self._tracks[person_id]
        return events
