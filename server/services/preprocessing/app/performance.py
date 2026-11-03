"""고정 크기 최근 표본으로 처리 구간별 지연을 요약한다."""

from __future__ import annotations

import math
import threading
from collections import defaultdict, deque


class TimingWindow:
    def __init__(self, capacity: int = 100):
        self._values: dict[str, deque[float]] = defaultdict(
            lambda: deque(maxlen=capacity)
        )
        self._lock = threading.Lock()

    def record(self, stage: str, seconds: float) -> None:
        if not math.isfinite(seconds) or seconds < 0:
            return
        with self._lock:
            self._values[stage].append(seconds)

    def snapshot(self) -> dict[str, dict[str, float | int]]:
        with self._lock:
            values = {stage: list(samples) for stage, samples in self._values.items()}
        result = {}
        for stage, samples in values.items():
            if not samples:
                continue
            ordered = sorted(samples)
            result[stage] = {
                "samples": len(samples),
                "last_seconds": samples[-1],
                "p50_seconds": ordered[int((len(ordered) - 1) * 0.50)],
                "p95_seconds": ordered[int((len(ordered) - 1) * 0.95)],
            }
        return result
