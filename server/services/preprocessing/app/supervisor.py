# Data의 활성 카메라 목록에 맞춰 카메라별 영상 처리 스레드를 관리한다.
# 카메라 추가·중지 여부를 주기적으로 확인하므로 목록 변경에 서비스를 다시 띄울 필요가 없다.
from __future__ import annotations

import logging
import threading
from typing import Any

from .data_client import DataClient
from .pipeline import CameraWorker
from .settings import Settings

LOGGER = logging.getLogger("ai_cctv.preprocessing")


class DetectionSupervisor:
    def __init__(self, settings: Settings, data_client: DataClient):
        self.settings = settings
        self.data_client = data_client
        self._workers: dict[str, CameraWorker] = {}
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._reconcile_loop, name="camera-supervisor", daemon=True
        )
        self.data_ready = False
        self.last_error: str | None = None

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=10)
        for worker in self._workers.values():
            worker.stop()
        for worker in self._workers.values():
            worker.join(timeout=10)
        self.data_client.close()

    def _reconcile_loop(self) -> None:
        while not self._stop.is_set():
            try:
                cameras = self.data_client.enabled_cameras()
                self.data_ready = True
                self.last_error = None
                self._reconcile(cameras)
            except Exception as exc:
                self.data_ready = False
                self.last_error = type(exc).__name__
                LOGGER.warning("camera reconciliation failed")
            self._stop.wait(self.settings.refresh_seconds)

    def _reconcile(self, cameras: list[dict[str, Any]]) -> None:
        # 원하는 목록과 실행 중인 목록의 차이를 맞춘다. 현재 설정 계약은 최대 4대다.
        desired = {str(camera["camera_id"]): camera for camera in cameras[:4]}
        for camera_id in set(self._workers) - set(desired):
            worker = self._workers.pop(camera_id)
            worker.stop()
            worker.join(timeout=10)

        for camera_id, camera in desired.items():
            # 이미 살아 있는 스레드는 유지하고, 새 카메라나 종료된 스레드만 시작한다.
            current = self._workers.get(camera_id)
            if current is not None and current.is_alive():
                continue
            worker = CameraWorker(camera, self.settings, self.data_client)
            self._workers[camera_id] = worker
            worker.start()

    def status(self) -> dict[str, Any]:
        return {
            "data_ready": self.data_ready,
            "last_error": self.last_error,
            "workers": {
                camera_id: vars(worker.status)
                for camera_id, worker in sorted(self._workers.items())
            },
        }
