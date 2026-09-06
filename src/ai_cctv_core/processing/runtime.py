"""Async lifecycle for a single scoped object job consumer."""

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
    plugin = await asyncio.to_thread(lambda: load_factory(plugin_reference)())
    if not callable(getattr(plugin, "process", None)):
        raise ValueError("Object processor must implement process(job, crop_path)")
    async with httpx.AsyncClient(
        base_url=data_url,
        headers={"X-Internal-Token": token},
        trust_env=False,
        timeout=10,
    ) as client:
        worker = ObjectWorker(stage, client, snapshots_root, plugin)
        task = asyncio.create_task(worker.run(), name=f"{stage}-jobs")
        try:
            yield worker
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass


def worker_status(worker: ObjectWorker | None) -> dict:
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
