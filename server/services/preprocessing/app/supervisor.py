# Data의 활성 카메라 목록에 맞춰 카메라별 영상 처리 스레드를 관리한다.
# 카메라 추가·중지 여부를 주기적으로 확인하므로 목록 변경에 서비스를 다시 띄울 필요가 없다.
from __future__ import annotations

import logging
import threading
import time
from typing import Any

from .data_client import DataClient
from .event_publisher import EventPublisher
from .pipeline import CameraWorker
from .settings import Settings

LOGGER = logging.getLogger("ai_cctv.preprocessing")


# Data의 활성 카메라 목록을 기준으로 영상 작업자를 추가·정리·재시작한다.
class DetectionSupervisor:
    def __init__(self, settings: Settings, data_client: DataClient):
        self.settings = settings
        self.data_client = data_client
        self._workers: dict[str, CameraWorker] = {}
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._reconcile_loop, name="camera-supervisor", daemon=True
        )
        self.data_ready = False
        self.last_error: str | None = None
        self.events = EventPublisher(
            data_client,
            settings.snapshots_root / ".event-outbox.sqlite3",
            settings.event_outbox_max_pending,
            settings.event_outbox_max_bytes,
        )

    def start(self) -> None:
        self.events.start()
        self._thread.start()

    # 목록 갱신과 작업자에 종료를 요청하고 제한 시간까지 기다린 뒤 공유 HTTP 연결을 닫는다.
    def stop(self) -> None:
        deadline = time.monotonic() + self.settings.shutdown_timeout_seconds
        self._stop.set()
        with self._lock:
            workers = list(self._workers.values())
            for worker in workers:
                worker.stop()
        if self._thread.ident is not None:
            self._thread.join(timeout=max(0, deadline - time.monotonic()))
        for worker in workers:
            if worker.ident is not None:
                worker.join(timeout=max(0, deadline - time.monotonic()))
        self.events.close(timeout=max(0, deadline - time.monotonic()))
        if any(worker.is_alive() for worker in workers):
            LOGGER.warning("camera worker did not stop before the shutdown deadline")
        self.data_client.close()

    # 목록 조회 실패 시 기존 작업자를 유지하고 다음 갱신 주기에 다시 조회한다.
    def _reconcile_loop(self) -> None:
        while not self._stop.is_set():
            try:
                cameras = self.data_client.enabled_cameras()
                self._reconcile(cameras)
                self.data_ready = True
                self.last_error = None
            except Exception as exc:
                self.data_ready = False
                self.last_error = type(exc).__name__
                LOGGER.warning("camera reconciliation failed")
            self._stop.wait(self.settings.refresh_seconds)

    def _reconcile(self, cameras: list[dict[str, Any]]) -> None:
        # 원하는 목록과 실행 중인 목록의 차이를 맞춘다. 현재 설정 계약은 최대 4대다.
        from ai_cctv_core.identifiers import validate_camera_id

        if not isinstance(cameras, list) or len(cameras) > 4:
            raise ValueError("enabled cameras must be a list of at most four items")
        desired = {}
        for camera in cameras:
            camera_id = validate_camera_id(camera["camera_id"])
            if camera_id in desired:
                raise ValueError("enabled cameras contain duplicate IDs")
            self.settings.rtsp_source_url(camera.get("stream_path") or camera_id)
            desired[camera_id] = camera
        with self._lock:
            if self._stop.is_set():
                return
            for camera_id, worker in list(self._workers.items()):
                camera = desired.get(camera_id)
                changed = camera is not None and worker.stream_path != (
                    camera.get("stream_path") or camera_id
                )
                if camera is None or changed:
                    worker.stop()
                if not worker.is_alive():
                    del self._workers[camera_id]

            for camera_id, camera in desired.items():
                # 종료를 요청한 작업자도 실제로 끝나기 전에는 대체하지 않아 같은 카메라의
                # 영상 읽기·모델 실행 스레드가 누적되지 않게 한다.
                if camera_id in self._workers:
                    continue
                worker = CameraWorker(
                    camera, self.settings, self.data_client, event_publisher=self.events
                )
                self._workers[camera_id] = worker
                worker.start()

    # 카메라 ID 순서가 일정한 작업자 상태와 마지막 목록 조회 결과를 반환한다.
    def status(self) -> dict[str, Any]:
        with self._lock:
            workers = {
                camera_id: worker.status_snapshot()
                for camera_id, worker in sorted(self._workers.items())
            }
        return {
            "data_ready": self.data_ready,
            "last_error": self.last_error,
            "workers": workers,
            "event_delivery": self.events.status(),
        }
