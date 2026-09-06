"""Internal notifications API."""

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


@router.post("/push-deliveries/claim")
def claim_push_delivery(repository: Repo) -> dict[str, Any]:
    return {"delivery": repository.claim_push()}


@router.post("/push-deliveries/{delivery_id}/complete")
def complete_push_delivery(
    delivery_id: int, payload: PushCompletion, repository: Repo
) -> dict[str, bool]:
    return {
        "accepted": repository.complete_push(
            delivery_id, payload.lease_id, payload.outcome, payload.error_code
        )
    }
