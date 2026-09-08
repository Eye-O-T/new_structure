"""Async lifecycle for a single scoped object job consumer."""

# 서비스가 시작될 때 작업자를 띄우고 종료할 때 정리하는 공통 수명 주기 관리 코드다.
# identity와 analysis는 각자 권한이 제한된 토큰으로 자신의 작업만 요청한다.

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import httpx

from .plugins import load_factory
from .worker import ObjectWorker


@asynccontextmanager
async def running_worker(
    stage, token, data_url, snapshots_root: Path, plugin_reference
):
    if len(token) < 32:
        raise ValueError(f"DATA_{stage.upper()}_TOKEN requires at least 32 characters")
    # 모델 준비는 오래 걸릴 수 있어 별도 스레드에서 실행하고 HTTP 처리 흐름을 막지 않는다.
    plugin = await asyncio.to_thread(lambda: load_factory(plugin_reference)())
    if not callable(getattr(plugin, "process", None)):
        raise ValueError("Object processor must implement process(job, crop_path)")
    async with httpx.AsyncClient(
        base_url=data_url,
        headers={"X-Internal-Token": token},
        # 시스템 프록시 설정으로 내부 인증 토큰이 다른 서버에 전달되는 것을 방지한다.
        trust_env=False,
        timeout=10,
    ) as client:
        worker = ObjectWorker(stage, client, snapshots_root, plugin)
        task = asyncio.create_task(worker.run(), name=f"{stage}-jobs")
        try:
            yield worker
        finally:
            # 서비스 종료 시 반복 작업에도 취소를 전달하고 정리가 끝날 때까지 기다린다.
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


def worker_status(worker: ObjectWorker | None) -> dict:
    # 준비 여부와 마지막 처리 결과를 구분한다. 모델 미구현(unconfigured)은
    # 작업 서버에 연결할 수 없는 장애와 다른 상태다.
    if worker is None:
        return {
            "ready": False,
            "stalled": False,
            "last_error": None,
            "last_outcome": None,
        }
    return {
        "ready": worker.ready,
        "stalled": worker.stalled,
        "last_error": worker.last_error,
        "last_outcome": worker.last_outcome,
    }
