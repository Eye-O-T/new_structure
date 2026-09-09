# 로그인한 사용자 자신의 단말만 알림 수신 대상으로 등록하거나 해제하도록 한다.

from __future__ import annotations

import hashlib
import hmac
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Response
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from ..clients.data import DataClient, DataNotFound
from ..config import Settings
from ..dependencies import get_data_client, get_settings_dependency
from ..security.permissions import Principal, get_current_principal
from ..security.tokens import TokenValidationError, decode_token


class DeviceRegistration(BaseModel):
    model_config = ConfigDict(extra="forbid")
    device_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    token: SecretStr = Field(min_length=20, max_length=4096)
    refresh_token: SecretStr = Field(min_length=1, max_length=8192)
    platform: Literal["android", "ios"]
    enabled: bool
    # null은 모든 이벤트, 빈 목록은 수신 안 함을 뜻한다. 앱에서 선택한 설정을 그대로 받는다.
    event_types: list[str] | None = Field(max_length=100)

    # FCM 단말 토큰에 공백이 섞여 전송 시 다른 값으로 해석되지 않게 한다.
    @field_validator("token")
    @classmethod
    def token_single_line(cls, value: SecretStr) -> SecretStr:
        if any(c.isspace() for c in value.get_secret_value()):
            raise ValueError("Invalid device token")
        return value

    # 확장 이벤트도 식별자 형식을 확인하고 정렬·중복 제거하되 None과 빈 목록은 구분한다.
    @field_validator("event_types")
    @classmethod
    def valid_event_types(cls, value: list[str] | None) -> list[str] | None:
        import re

        if value is not None and any(
            not re.fullmatch(r"[a-z][a-z0-9_]{0,127}", item) for item in value
        ):
            raise ValueError("Invalid event type")
        return sorted(set(value)) if value is not None else None


router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


@router.get("/status")
async def push_status(
    principal: Principal = Depends(get_current_principal),
    settings: Settings = Depends(get_settings_dependency),
) -> dict:
    return {"enabled": settings.push_enabled, "provider": "fcm"}


# 접근 주체와 제출한 갱신 세션의 소유자·해시를 대조한 뒤 로그인 계열에 단말을 등록한다.
@router.put("/devices")
async def register_device(
    payload: DeviceRegistration,
    response: Response,
    principal: Principal = Depends(get_current_principal),
    settings: Settings = Depends(get_settings_dependency),
    data: DataClient = Depends(get_data_client),
) -> dict:
    encoded = payload.refresh_token.get_secret_value()
    # FCM 단말 토큰만으로는 소유자를 알 수 없어 현재 사용자의 로그인 세션까지 함께 확인한다.
    try:
        claims = decode_token(encoded, settings, expected_type="refresh")
        record = await data.get_refresh_token(claims.jti)
    except (TokenValidationError, DataNotFound) as exc:
        raise HTTPException(401, "Invalid refresh session") from exc
    token_hash = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    if (
        claims.sub != principal.user_id
        or str(record.get("user_id")) != principal.user_id
        or record.get("revoked_at") is not None
        or record.get("replaced_by_jti") is not None
        or not hmac.compare_digest(str(record.get("token_hash", "")), token_hash)
    ):
        raise HTTPException(401, "Invalid refresh session")
    response.headers["Cache-Control"] = "no-store"
    return await data.put_mobile_device(
        {
            "device_id": payload.device_id,
            "user_id": int(principal.user_id),
            "refresh_jti": claims.jti,
            "token": payload.token.get_secret_value(),
            "platform": payload.platform,
            "enabled": payload.enabled,
            "event_types": payload.event_types,
        }
    )


# 현재 사용자의 ID를 함께 보내 다른 계정의 단말 등록은 삭제할 수 없게 한다.
@router.delete("/devices/{device_id}", status_code=204)
async def unregister_device(
    device_id: str = Path(pattern=r"^[a-f0-9]{32}$"),
    principal: Principal = Depends(get_current_principal),
    data: DataClient = Depends(get_data_client),
) -> Response:
    await data.delete_mobile_device(device_id, principal.user_id)
    return Response(status_code=204, headers={"Cache-Control": "no-store"})
