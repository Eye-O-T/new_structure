# 반복 로그인 실패 시 다음 시도까지 대기 시간을 늘려 비밀번호 무차별 대입을 늦춘다.
from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass


@dataclass
class _AttemptState:
    failures: int
    blocked_until: float


# 현재 프로세스의 접속 주소·계정별 실패 이력을 잠금으로 보호한다.
class LoginBackoff:
    def __init__(self, base_seconds: int, max_seconds: int) -> None:
        self.base_seconds = base_seconds
        self.max_seconds = max_seconds
        self._attempts: dict[str, _AttemptState] = {}
        self._lock = threading.Lock()

    # 시스템 시각 변경에 영향받지 않는 단조 시계로 Retry-After의 남은 초를 올림 계산한다.
    def retry_after(self, key: str) -> int:
        now = time.monotonic()
        with self._lock:
            state = self._attempts.get(key)
            if state is None or state.blocked_until <= now:
                return 0
            return max(1, math.ceil(state.blocked_until - now))

    # 실패 횟수를 제한하면서 대기 시간을 두 배씩 늘리고 설정한 최대값에서 멈춘다.
    def record_failure(self, key: str) -> int:
        now = time.monotonic()
        with self._lock:
            previous = self._attempts.get(key)
            failures = 1 if previous is None else min(previous.failures + 1, 32)
            delay = min(self.max_seconds, self.base_seconds * (2 ** (failures - 1)))
            self._attempts[key] = _AttemptState(
                failures=failures,
                blocked_until=now + delay,
            )
            return delay

    # 정상 로그인한 키의 누적 실패 이력을 제거한다.
    def clear(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)
