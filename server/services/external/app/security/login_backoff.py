# 반복 로그인 실패 시 다음 시도까지 대기 시간을 늘려 비밀번호 무차별 대입을 늦춘다.
from __future__ import annotations

import math
import threading
import time
from collections import OrderedDict, deque
from dataclasses import dataclass


@dataclass
class _AttemptState:
    failures: int
    blocked_until: float
    updated_at: float


# 현재 프로세스의 접속 주소·계정별 실패 이력을 잠금으로 보호한다.
class LoginBackoff:
    def __init__(
        self,
        base_seconds: int,
        max_seconds: int,
        *,
        max_entries: int = 10_000,
        idle_seconds: float | None = None,
        address_max_failures: int = 20,
        address_window_seconds: float = 60,
    ) -> None:
        if base_seconds <= 0 or max_seconds < base_seconds or max_entries <= 0:
            raise ValueError("invalid login backoff limits")
        idle_seconds = max(900, max_seconds) if idle_seconds is None else idle_seconds
        if not math.isfinite(idle_seconds) or idle_seconds < max_seconds:
            raise ValueError("idle expiry must cover the maximum backoff")
        if (
            address_max_failures <= 0
            or not math.isfinite(address_window_seconds)
            or address_window_seconds <= 0
        ):
            raise ValueError("invalid address failure limits")
        self.base_seconds = base_seconds
        self.max_seconds = max_seconds
        self.max_entries = max_entries
        self.idle_seconds = idle_seconds
        self.address_max_failures = address_max_failures
        self.address_window_seconds = address_window_seconds
        # 실패 갱신 순서대로 보관하여 정리는 오래된 항목에만 비례한다.
        self._attempts: OrderedDict[str, _AttemptState] = OrderedDict()
        self._addresses: OrderedDict[str, deque[float]] = OrderedDict()
        self._lock = threading.Lock()

    def _expire(self, now: float) -> None:
        while self._attempts:
            key, state = next(iter(self._attempts.items()))
            if now - state.updated_at < self.idle_seconds:
                break
            del self._attempts[key]
        while self._addresses:
            address, failures = next(iter(self._addresses.items()))
            if now - failures[-1] < self.address_window_seconds:
                break
            del self._addresses[address]

    def _address_delay(self, address: str | None, now: float) -> int:
        failures = self._addresses.get(address) if address is not None else None
        if not failures:
            return 0
        cutoff = now - self.address_window_seconds
        while failures and failures[0] <= cutoff:
            failures.popleft()
        if len(failures) < self.address_max_failures:
            return 0
        return max(1, math.ceil(failures[0] + self.address_window_seconds - now))

    # 시스템 시각 변경에 영향받지 않는 단조 시계로 Retry-After의 남은 초를 올림 계산한다.
    def retry_after(self, key: str, *, address: str | None = None) -> int:
        now = time.monotonic()
        with self._lock:
            self._expire(now)
            state = self._attempts.get(key)
            account_delay = (
                0
                if state is None or state.blocked_until <= now
                else max(1, math.ceil(state.blocked_until - now))
            )
            return max(account_delay, self._address_delay(address, now))

    # 실패 횟수를 제한하면서 대기 시간을 두 배씩 늘리고 설정한 최대값에서 멈춘다.
    def record_failure(self, key: str, *, address: str | None = None) -> int:
        now = time.monotonic()
        with self._lock:
            self._expire(now)
            previous = self._attempts.get(key)
            failures = 1 if previous is None else min(previous.failures + 1, 32)
            delay = min(self.max_seconds, self.base_seconds * (2 ** (failures - 1)))
            self._attempts[key] = _AttemptState(
                failures=failures,
                blocked_until=now + delay,
                updated_at=now,
            )
            self._attempts.move_to_end(key)
            while len(self._attempts) > self.max_entries:
                self._attempts.popitem(last=False)
            if address is not None:
                timestamps = self._addresses.setdefault(
                    address,
                    deque(maxlen=self.address_max_failures),
                )
                timestamps.append(now)
                self._addresses.move_to_end(address)
                while len(self._addresses) > self.max_entries:
                    self._addresses.popitem(last=False)
            return max(delay, self._address_delay(address, now))

    # 정상 로그인한 계정 이력만 지운다. 주소 이력은 계정을 바꿔 우회하지 못하게 만료까지 유지한다.
    def clear(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)
