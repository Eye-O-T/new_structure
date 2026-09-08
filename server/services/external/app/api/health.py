# External 실행 상태와 Data 연결 상태를 확인하고 사용자에게 시스템 상태를 제공한다.
from __future__ import annotations

from typing import Any

from fastapi import (
    APIRouter,
    Depends,
)

from ..clients.data import (
    DataClient,
)
from ..dependencies import (
    get_data_client,
)
from ..schemas import (
    SystemStatusResponse,
)
from ..security.permissions import Principal, require_admin
from ..version import SERVICE_VERSION

router = APIRouter()


@router.get("/health/live", include_in_schema=False)
async def health_live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/health/ready", include_in_schema=False)
async def health_ready(
    data: DataClient = Depends(get_data_client),
) -> dict[str, Any]:
    await data.health()
    return {"status": "ready"}


@router.get("/api/v1/system/status", response_model=SystemStatusResponse)
@router.get("/api/v1/admin/system/status", response_model=SystemStatusResponse)
async def system_status(
    _: Principal = Depends(require_admin),
    data: DataClient = Depends(get_data_client),
) -> Any:
    data_status = await data.health()
    return {
        "external": {"status": "running", "version": SERVICE_VERSION},
        "data": data_status,
    }
