from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from fastapi import Depends, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from .config import Settings
from .data_client import DataClient, DataNotFound
from .security import (
    LoginBackoff,
    TokenExpiredError,
    TokenValidationError,
    decode_token,
)


@dataclass(frozen=True)
class Principal:
    user_id: str
    username: str
    role: Literal["admin", "viewer"]
    access_jti: str
    access_exp: int


_bearer = HTTPBearer(auto_error=False)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()


def get_settings_dependency() -> Settings:
    return get_settings()


async def get_data_client(
    request: Request,
    settings: Settings = Depends(get_settings_dependency),
) -> DataClient:
    existing = getattr(request.app.state, "data_client", None)
    if existing is not None:
        return existing

    client = DataClient(
        base_url=settings.data_base_url,
        health_url=settings.data_health_url,
        internal_token=settings.internal_token,
    )
    request.app.state.data_client = client
    return client


def get_login_backoff(
    request: Request,
    settings: Settings = Depends(get_settings_dependency),
) -> LoginBackoff:
    existing = getattr(request.app.state, "login_backoff", None)
    if existing is not None:
        return existing

    backoff = LoginBackoff(
        settings.login_backoff_base_seconds,
        settings.login_backoff_max_seconds,
    )
    request.app.state.login_backoff = backoff
    return backoff


def _unauthorized(detail: str = "Authentication required") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


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

    if await data.is_access_token_revoked(claims.jti):
        raise _unauthorized("Access token revoked")

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


def require_admin(
    principal: Principal = Depends(get_current_principal),
) -> Principal:
    if principal.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Admin role required"
        )
    return principal
