# 서비스가 시작될 때 작업자를 띄우고 종료할 때 정리하는 공통 수명 주기 관리 코드다.
# identity와 analysis는 각자 권한이 제한된 토큰으로 자신의 작업만 요청한다.

import asyncio
import math
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from .isolation import IsolatedProcessor
from .worker import ObjectWorker


# 플러그인과 내부 HTTP 클라이언트를 준비하고 서비스 수명 동안 작업자를 실행한다.
@asynccontextmanager
async def running_worker(
    stage, token, data_url, snapshots_root: Path, plugin_reference
):
    if len(token) < 32:
        raise ValueError(f"DATA_{stage.upper()}_TOKEN requires at least 32 characters")
    timeout = float(os.getenv("OBJECT_MODEL_TIMEOUT_SECONDS", "120"))
    startup = float(os.getenv("OBJECT_STARTUP_TIMEOUT_SECONDS", "30"))
    if not math.isfinite(timeout) or not 0 < timeout <= 240:
        raise ValueError("OBJECT_MODEL_TIMEOUT_SECONDS must be in (0,240]")
    if not math.isfinite(startup) or not 0 < startup <= 120:
        raise ValueError("OBJECT_STARTUP_TIMEOUT_SECONDS must be in (0,120]")
    # 초기화도 유한하게 기다린다. 취소될 때 생성 중인 자식을 놓치지 않도록 task를 보존한다.
    initialization_stop = threading.Event()
    initializing = asyncio.create_task(
        asyncio.to_thread(
            IsolatedProcessor,
            plugin_reference,
            timeout_seconds=timeout,
            startup_timeout_seconds=startup,
            stop_event=initialization_stop,
        )
    )
    try:
        plugin = await asyncio.shield(initializing)
    except asyncio.CancelledError:
        initialization_stop.set()
        try:
            plugin = await initializing
        except Exception:
            pass
        else:
            await asyncio.to_thread(plugin.close)
        raise
    try:
        async with httpx.AsyncClient(
            base_url=data_url,
            headers={"X-Internal-Token": token},
            # 내부 토큰이 시스템 프록시로 전달되지 않게 한다.
            trust_env=False,
            timeout=10,
        ) as client:
            # 자식 종료에 필요한 짧은 여유를 바깥 watchdog에 포함한다.
            worker = ObjectWorker(stage, client, snapshots_root, plugin, timeout + 5)
            task = asyncio.create_task(worker.run(), name=f"{stage}-jobs")
            try:
                yield worker
            finally:
                task.cancel()
                await asyncio.to_thread(plugin.close)
                try:
                    await task
                except asyncio.CancelledError:
                    pass
    finally:
        await asyncio.to_thread(plugin.close)


def worker_status(worker: ObjectWorker | None) -> dict:
    # 준비 여부와 마지막 처리 결과를 구분한다. 모델 미구현(unconfigured)은
    # 작업 서버에 연결할 수 없는 장애와 다른 상태다.
    if worker is None:
        return {
            "ready": False,
            "stalled": False,
            "last_error": None,
            "last_outcome": None,
            "backend": None,
            "model_ready": False,
        }
    return {
        "ready": worker.ready,
        "stalled": worker.stalled,
        "last_error": worker.last_error,
        "last_outcome": worker.last_outcome,
        "backend": getattr(worker.plugin, "reference", type(worker.plugin).__name__),
        "model_ready": not worker.stalled and worker.last_outcome != "unconfigured",
    }
