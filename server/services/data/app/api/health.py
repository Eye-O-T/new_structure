# 프로세스 생존 여부와 DB·저장소 사용 가능 여부를 서로 다른 점검으로 제공한다.

from __future__ import annotations

from typing import Any

from fastapi import (
    APIRouter,
    Request,
)
from fastapi.responses import JSONResponse

from ..dependencies import Repo, RuntimeSettings
from ..errors import ApiError
from ..storage.retention import storage_is_ready, storage_usage
from ..storage.protection import read_snapshot_protection

router = APIRouter()


@router.get("/health/live")
def health_live() -> dict[str, str]:
    return {"status": "alive"}


# DB 쿼리와 영속 폴더 접근을 모두 확인해야 준비 완료로 응답한다.
@router.get("/health/ready")
def health_ready(request: Request, repository: Repo, settings: RuntimeSettings) -> Any:
    try:
        database = repository.database.health()
        queues = repository.queue_counts()
        queue_metrics = repository.queue_metrics()
    except Exception as exc:
        raise ApiError(
            503, "DATABASE_NOT_READY", "SQLite를 사용할 수 없습니다."
        ) from exc
    if not storage_is_ready(settings):
        raise ApiError(503, "STORAGE_NOT_READY", "영속 저장소를 사용할 수 없습니다.")
    workers = {
        name: state.snapshot()
        for name, state in getattr(request.app.state, "workers", {}).items()
    }
    for name, task in getattr(request.app.state, "worker_tasks", {}).items():
        if task.done() and name in workers:
            workers[name].update(
                alive=False, status="error", last_error="WORKER_STOPPED"
            )
    ready = all(
        worker["alive"] and worker["status"] != "error" for worker in workers.values()
    )
    body = {
        "status": "ready" if ready else "degraded",
        "database": database,
        "storage": storage_usage(settings),
        "workers": workers,
        "queues": queues,
        "queue_metrics": queue_metrics,
        "retention": read_snapshot_protection(settings).status(),
    }
    return JSONResponse(status_code=200 if ready else 503, content=body)
