# 내부 API에 공통 인증을 적용하고 분야별 요청 처리기를 연결한다.

from fastapi import APIRouter, Depends

from ..security import require_internal_token
from . import (
    cameras,
    events,
    maintenance,
    notifications,
    objects,
    recordings,
    sessions,
    users,
)


def build_internal_router() -> APIRouter:
    router = APIRouter(
        prefix="/internal/v1", dependencies=[Depends(require_internal_token)]
    )
    for module in (
        objects,
        notifications,
        users,
        cameras,
        recordings,
        events,
        maintenance,
        sessions,
    ):
        router.include_router(module.router)
    return router
