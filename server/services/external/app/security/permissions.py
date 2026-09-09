# 토큰과 현재 계정 상태를 확인한 뒤 관리자 권한·카메라 열람 권한을 검사한다.

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..clients.data import DataClient, DataForbidden, DataNotFound, DataServiceError
from ..config import CAMERA_ID_PATTERN, Settings
from ..dependencies import get_data_client, get_settings_dependency
from .tokens import TokenExpiredError, TokenValidationError, decode_token

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    user_id: str
    username: str
    role: Literal["admin", "viewer"]
    access_jti: str
    access_exp: int


def _unauthorized(detail: str = "Authentication required") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


# Bearer를 쿠키보다 우선하고 JWT 검증 뒤 폐기 여부·현재 계정 역할까지 다시 확인한다.
async def get_current_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Security(_bearer),
    settings: Settings = Depends(get_settings_dependency),
    data: DataClient = Depends(get_data_client),
) -> Principal:
    token = credentials.credentials if credentials is not None else None
    if token is None:
        token = request.cookies.get(settings.access_cookie_name)
    if not token:
        raise _unauthorized()

    try:
        claims = decode_token(token, settings, expected_type="access")
    except TokenExpiredError as exc:
        raise _unauthorized("Access token expired") from exc
    except TokenValidationError as exc:
        raise _unauthorized("Invalid access token") from exc

    # 서명이 맞아도 로그아웃·계정 정지·권한 변경이 있었을 수 있어 현재 DB 상태를 다시 확인한다.
    if await data.is_access_token_revoked(claims.jti):
        raise _unauthorized("Access token revoked")
    if not await data.is_session_family_active(claims.session_id, claims.sub):
        raise _unauthorized("Login session revoked")

    try:
        user = await data.get_user(claims.sub)
    except DataNotFound as exc:
        raise _unauthorized("User is unavailable") from exc

    is_active = user.get("is_active", user.get("active", True))
    role = user.get("role", claims.role)
    if not is_active or role not in {"admin", "viewer"}:
        raise _unauthorized("User is inactive")
    if role != claims.role:
        raise _unauthorized("User role changed; authenticate again")

    user_id = str(user.get("id", user.get("user_id", claims.sub)))
    if user_id != claims.sub:
        raise _unauthorized("Invalid user identity")

    return Principal(
        user_id=user_id,
        username=str(user.get("username", "")),
        role=role,
        access_jti=claims.jti,
        access_exp=claims.exp,
    )


# 인증된 주체의 역할을 검사하여 관리자 전용 변경 API의 공통 경계로 사용한다.
def require_admin(
    principal: Principal = Depends(get_current_principal),
) -> Principal:
    if principal.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required"
        )
    return principal


# 목록·페이지 및 문자열·객체 형식의 권한 응답을 카메라 ID 집합으로 통일한다.
def _camera_ids_from_permissions(payload: Any) -> set[str]:
    if isinstance(payload, dict):
        items = payload.get("items", payload.get("camera_ids", []))
    else:
        items = payload
    if not isinstance(items, list):
        raise DataServiceError("invalid camera permission response")

    camera_ids: set[str] = set()
    for item in items:
        if isinstance(item, str):
            camera_ids.add(item)
        elif isinstance(item, dict) and item.get("camera_id") is not None:
            camera_ids.add(str(item["camera_id"]))
    return camera_ids


# 일반 사용자의 현재 카메라 권한을 확인한 뒤 실제 카메라 자료를 조회한다.
async def _ensure_camera_access(
    data: DataClient,
    principal: Principal,
    camera_id: str,
) -> dict[str, Any]:
    # 로그인 확인과 카메라 접근 허용은 별개다. 일반 사용자는 배정된 카메라만 열람할 수 있다.
    if not CAMERA_ID_PATTERN.fullmatch(camera_id):
        raise HTTPException(status_code=400, detail="Invalid camera ID")
    if principal.role != "admin":
        permissions = await data.get_camera_permissions(principal.user_id)
        if camera_id not in _camera_ids_from_permissions(permissions):
            raise DataForbidden("camera access denied")
    return await data.get_camera(camera_id, user_id=principal.user_id)
