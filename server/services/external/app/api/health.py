# External 실행 상태와 Data 연결 상태를 확인하고 사용자에게 시스템 상태를 제공한다.
from __future__ import annotations

from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    Request,
    Response,
)

from ..clients.data import (
    DataClient,
)
from ..dependencies import (
    get_data_client,
    get_settings_dependency,
)
from ..config import Settings
from ..diagnostics import system_diagnostics
from ..schemas import (
    SystemStatusResponse,
)
from ..security.permissions import Principal, require_admin
from ..version import SERVICE_VERSION

router = APIRouter()


@router.get("/health/live", include_in_schema=False)
async def health_live() -> dict[str, str]:
    return {"status": "ok"}


# Data가 준비되어 있어야 공개 API 서비스도 요청을 처리할 준비가 된 것으로 판단한다.
@router.get("/health/ready", include_in_schema=False)
async def health_ready(
    data: DataClient = Depends(get_data_client),
) -> dict[str, Any]:
    await data.health()
    return {"status": "ready"}


# 관리자에게 External 버전과 실제 Data 준비 상태를 함께 제공한다.
@router.get("/api/v1/system/status", response_model=SystemStatusResponse)
@router.get("/api/v1/admin/system/status", response_model=SystemStatusResponse)
async def system_status(
    request: Request,
    response: Response,
    _: Principal = Depends(require_admin),
    data: DataClient = Depends(get_data_client),
    settings: Settings = Depends(get_settings_dependency),
) -> Any:
    diagnostics = await system_diagnostics(settings, data)
    dispatcher = getattr(request.app.state, "push_dispatcher", None)
    push = (
        {"enabled": False, "status": "disabled", "delivery_confirmed": False}
        if not settings.push_enabled else
        dispatcher.status() if dispatcher is not None else
        {"enabled": True, "status": "unavailable", "delivery_confirmed": False,
         "last_error_code": "PUSH_DISPATCHER_UNAVAILABLE"}
    )
    push["queues"] = diagnostics["data"].get("queues", {}).get("push", {})
    push["queue_metrics"] = diagnostics["data"].get("queue_metrics", {}).get("push", {})
    degraded = any(
        component["status"] not in {"ready", "disabled", "waiting"}
        for component in (*diagnostics.values(), push)
    )
    response.headers["Cache-Control"] = "no-store"
    return {
        "status": "degraded" if degraded else "ready",
        "external": {"status": "running", "version": SERVICE_VERSION},
        **diagnostics,
        "push": push,
    }
