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


# 하위 라우터 전체에 내부 토큰 의존성을 적용하여 개별 등록 누락으로 인증이 빠지지 않게 한다.
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
