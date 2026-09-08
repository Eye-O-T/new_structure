# 프로세스 생존 여부와 DB·저장소 사용 가능 여부를 서로 다른 점검으로 제공한다.

from __future__ import annotations

from typing import Any

from fastapi import (
    APIRouter,
)

from ..dependencies import Repo, RuntimeSettings
from ..errors import ApiError
from ..storage.retention import storage_is_ready, storage_usage

router = APIRouter()


@router.get("/health/live")
def health_live() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/health/ready")
def health_ready(repository: Repo, settings: RuntimeSettings) -> dict[str, Any]:
    try:
        database = repository.database.health()
    except Exception as exc:
        raise ApiError(
            503, "DATABASE_NOT_READY", "SQLite를 사용할 수 없습니다."
        ) from exc
    if not storage_is_ready(settings):
        raise ApiError(503, "STORAGE_NOT_READY", "영속 저장소를 사용할 수 없습니다.")
    return {
        "status": "ready",
        "database": database,
        "storage": storage_usage(settings),
    }
