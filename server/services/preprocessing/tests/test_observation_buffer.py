from datetime import datetime, timedelta, timezone

import numpy as np

from server.services.preprocessing.app.event_state import TrackEvent
from server.services.preprocessing.app.observation_buffer import ObservationBuffer


def detection(person="1", box=(0, 0, 40, 80)):
    return {"person_id": person, "bbox": box, "confidence": 0.9}


def test_later_usable_crop_replaces_small_first_crop_and_preserves_appearance_time():
    buffer = ObservationBuffer(1, 1024 * 1024)
    first = datetime.now(timezone.utc)
    blank = np.zeros((80, 40, 3), dtype=np.uint8)
    textured = np.random.default_rng(1).integers(0, 256, blank.shape, dtype=np.uint8)
    transitions = [TrackEvent("person_appeared", "1", 0.9)]
    assert (
        buffer.update(blank, [detection(box=(0, 0, 8, 8))], transitions, 0, first) == []
    )
    selected = buffer.update(
        textured, [detection()], [], 1, first + timedelta(seconds=1)
    )
    assert len(selected) == 1
    assert selected[0].occurred_at == first
    assert selected[0].selected_at == first + timedelta(seconds=1)
    assert selected[0].detection["bbox"] == (0, 0, 40, 80)
    assert selected[0].score[0] == 1
    assert not buffer.pending


def test_departure_flushes_even_insufficient_candidate_before_its_window_ends():
    buffer = ObservationBuffer(1, 1024 * 1024)
    image = np.zeros((80, 40, 3), dtype=np.uint8)
    stamp = datetime.now(timezone.utc)
    buffer.update(
        image, [detection()], [TrackEvent("person_appeared", "1", 0.9)], 0, stamp
    )
    selected = buffer.update(
        image, [], [TrackEvent("person_disappeared", "1", 0.9)], 0.5, stamp
    )
    assert len(selected) == 1 and selected[0].score[0] == 0


def test_buffer_limits_memory_and_frames_are_shared_between_candidates():
    image = np.zeros((80, 40, 3), dtype=np.uint8)
    stamp = datetime.now(timezone.utc)
    buffer = ObservationBuffer(1, image.nbytes)
    events = [TrackEvent("person_appeared", str(i), 0.9) for i in range(10)]
    assert (
        buffer.update(image, [detection(str(i)) for i in range(10)], events, 0, stamp)
        == []
    )
    assert buffer.bytes_used == image.nbytes
    limited = ObservationBuffer(1, image.nbytes - 1)
    selected = limited.update(image, [detection()], events[:1], 0, stamp)
    # ID 0인 등장과 ID 1인 detection은 일치하지 않아 후보를 만들지 않는다.
    assert not selected
    selected = limited.update(
        image, [detection()], [TrackEvent("person_appeared", "1", 0.9)], 0, stamp
    )
    assert len(selected) == 1 and limited.bytes_used == 0
