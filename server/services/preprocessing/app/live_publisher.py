# 실시간 박스는 오래된 자료를 모두 보내기보다 가장 최근 상태를 빨리 보내는 것이 중요하다.
# HTTP 전송을 별도 스레드에 맡겨 느린 네트워크가 카메라 탐지를 막지 않게 한다.

import queue
import threading
import logging
import time
from datetime import datetime, timezone

from ai_cctv_core.contracts.objects import LIVE_OBJECTS_MAX_AGE_SECONDS
from ai_cctv_core.time import parse_utc, utc_now

LOGGER = logging.getLogger("ai_cctv.preprocessing")

from .performance import TimingWindow


# 최신 좌표 한 건만 보관하는 비동기 전송기로 탐지 스레드의 네트워크 대기를 줄인다.
class LivePublisher:
    def __init__(self, data_client, camera_id):
        self.data = data_client
        self.camera_id = camera_id
        # 대기 공간을 한 칸만 두고, 전송 중 새 결과가 쌓이면 가장 최신 결과만 남긴다.
        self.pending = queue.Queue(maxsize=1)
        self.stopped = threading.Event()
        self._status_lock = threading.Lock()
        self._last_success_at = None
        self._last_success_observed_age_seconds = None
        self._last_success_monotonic = None
        self._last_error_code = None
        self._consecutive_failures = 0
        self._reported_error_code = None
        self._timings = TimingWindow()
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    # 생산자를 기다리게 하지 않고 전송 대기 중인 좌표를 최신 관측으로 교체한다.
    def submit(self, payload):
        if self.stopped.is_set():
            return
        try:
            self.pending.put_nowait(payload)
        except queue.Full:
            # 아직 전송하지 않은 이전 좌표를 버린 뒤 새 좌표로 교체한다.
            try:
                self.pending.get_nowait()
            except queue.Empty:
                pass
            try:
                self.pending.put_nowait(payload)
            except queue.Full:
                pass

    def run(self):
        while not self.stopped.is_set():
            try:
                payload = self.pending.get(timeout=0.5)
            except queue.Empty:
                continue
            if self.stopped.is_set():
                break
            started = time.monotonic()
            timings = {}
            try:
                timings = self.data.put_live_objects(self.camera_id, payload) or {}
                for key in ("db_lock_wait_seconds", "db_transaction_seconds"):
                    value = timings.get(key)
                    if isinstance(value, (int, float)):
                        self._timings.record(key, value)
            except Exception as exc:
                code = self._error_code(exc)
                with self._status_lock:
                    self._last_error_code = code
                    self._consecutive_failures += 1
                    should_log = code != self._reported_error_code
                    self._reported_error_code = code
                if should_log:
                    LOGGER.warning(
                        "live object delivery failed: camera=%s error_code=%s elapsed_seconds=%.3f",
                        self.camera_id,
                        code,
                        time.monotonic() - started,
                    )
            else:
                with self._status_lock:
                    recovered = self._consecutive_failures > 0
                    self._last_success_at = datetime.now(timezone.utc).isoformat()
                    self._last_success_observed_age_seconds = (
                        utc_now() - parse_utc(payload["observed_at"])
                    ).total_seconds()
                    self._last_success_monotonic = time.monotonic()
                    self._last_error_code = None
                    self._consecutive_failures = 0
                    self._reported_error_code = None
                if recovered:
                    LOGGER.info(
                        "live object delivery recovered: camera=%s",
                        self.camera_id,
                    )
            finally:
                self._timings.record("data_put_seconds", time.monotonic() - started)

    @staticmethod
    def _error_code(exc):
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        if status is not None:
            return f"HTTP_{status}" if status in {401, 403, 404, 409, 422} else "HTTP_ERROR"
        name = type(exc).__name__.lower()
        if "timeout" in name:
            return "TIMEOUT"
        if "connect" in name or "network" in name:
            return "CONNECTION_ERROR"
        return "DELIVERY_ERROR"

    def status(self):
        with self._status_lock:
            observed_age = self._last_success_observed_age_seconds
            if observed_age is not None:
                observed_age += max(
                    0, time.monotonic() - self._last_success_monotonic
                )
            return {
                "last_success_at": self._last_success_at,
                "observed_age_seconds": observed_age,
                "stale": observed_age is not None
                and (
                    observed_age < 0
                    or observed_age > LIVE_OBJECTS_MAX_AGE_SECONDS
                ),
                "last_error_code": self._last_error_code,
                "consecutive_failures": self._consecutive_failures,
                "timings": self._timings.snapshot(),
            }

    # 종료를 요청하고 제한 시간만 기다린다. 남은 좌표를 모두 전송하는 보장은 하지 않는다.
    def close(self):
        self.stopped.set()
        self.thread.join(timeout=3)
