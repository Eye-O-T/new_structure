# 단말 등록 정보와 알림 대기열을 관리한다. 실제 FCM 발송은 External에 맡긴다.

from __future__ import annotations

from typing import Any

from fastapi import (
    APIRouter,
    Query,
    Response,
)

from ..dependencies import Repo
from ..errors import ApiError
from ..schemas import (
    MobileDevicePut,
    PushCompletion,
)

router = APIRouter()


# 저장소의 세션 소유권·유효성 실패를 단말 등록 거절로 변환한다.
@router.put("/mobile-devices")
def put_mobile_device(payload: MobileDevicePut, repository: Repo) -> dict[str, Any]:
    try:
        return repository.put_mobile_device(payload.model_dump())
    except PermissionError as exc:
        raise ApiError(403, "SESSION_UNAVAILABLE", "Session is unavailable") from exc


@router.delete("/mobile-devices/{device_id}", status_code=204)
def delete_mobile_device(
    device_id: str, repository: Repo, user_id: int = Query(gt=0)
) -> Response:
    repository.delete_mobile_device(device_id, user_id)
    return Response(status_code=204)


# 발송할 항목이 없으면 delivery를 null로 반환하여 작업자가 대기할 수 있게 한다.
@router.post("/push-deliveries/claim")
def claim_push_delivery(repository: Repo) -> dict[str, Any]:
    return {"delivery": repository.claim_push()}


# 임대 ID를 포함한 완료 보고를 전달하고 오래된 보고의 거절 여부도 반환한다.
@router.post("/push-deliveries/{delivery_id}/complete")
def complete_push_delivery(
    delivery_id: int, payload: PushCompletion, repository: Repo
) -> dict[str, bool]:
    return {
        "accepted": repository.complete_push(
            delivery_id, payload.lease_id, payload.outcome, payload.error_code
        )
    }
