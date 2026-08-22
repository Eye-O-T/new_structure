from __future__ import annotations

import math
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

import jwt
from argon2 import PasswordHasher
from jwt import ExpiredSignatureError, InvalidTokenError

from .config import Settings


Role = Literal["admin", "viewer"]
TokenType = Literal["access", "refresh"]

_PASSWORD_HASHER = PasswordHasher(
    time_cost=2,
    memory_cost=19_456,
    parallelism=1,
    hash_len=32,
    salt_len=16,
)


class TokenValidationError(Exception):
    pass


class TokenExpiredError(TokenValidationError):
    pass


@dataclass(frozen=True)
class TokenClaims:
    sub: str
    role: Role
    token_type: TokenType
    iat: int
    exp: int
    jti: str


@dataclass(frozen=True)
class IssuedToken:
    encoded: str
    claims: TokenClaims


@dataclass
class _AttemptState:
    failures: int
    blocked_until: float


class LoginBackoff:
    def __init__(self, base_seconds: int, max_seconds: int) -> None:
        self.base_seconds = base_seconds
        self.max_seconds = max_seconds
        self._attempts: dict[str, _AttemptState] = {}
        self._lock = threading.Lock()

    def retry_after(self, key: str) -> int:
        now = time.monotonic()
        with self._lock:
            state = self._attempts.get(key)
            if state is None or state.blocked_until <= now:
                return 0
            return max(1, math.ceil(state.blocked_until - now))

    def record_failure(self, key: str) -> int:
        now = time.monotonic()
        with self._lock:
            previous = self._attempts.get(key)
            failures = 1 if previous is None else min(previous.failures + 1, 32)
            delay = min(self.max_seconds, self.base_seconds * (2 ** (failures - 1)))
            self._attempts[key] = _AttemptState(
                failures=failures,
                blocked_until=now + delay,
            )
            return delay

    def clear(self, key: str) -> None:
        with self._lock:
            self._attempts.pop(key, None)


def hash_password(password: str) -> str:
    return _PASSWORD_HASHER.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return bool(_PASSWORD_HASHER.verify(password_hash, password))
    except Exception:
        return False


def issue_token(
    settings: Settings,
    *,
    user_id: str,
    role: Role,
    token_type: TokenType,
    ttl_seconds: int,
    now: datetime | None = None,
) -> IssuedToken:
    issued_at = now or datetime.now(timezone.utc)
    expires_at = issued_at + timedelta(seconds=ttl_seconds)
    claims = TokenClaims(
        sub=str(user_id),
        role=role,
        token_type=token_type,
        iat=int(issued_at.timestamp()),
        exp=int(expires_at.timestamp()),
        jti=uuid.uuid4().hex,
    )
    payload = {
        "sub": claims.sub,
        "role": claims.role,
        "type": claims.token_type,
        "iat": claims.iat,
        "exp": claims.exp,
        "jti": claims.jti,
        "iss": settings.jwt_issuer,
        "aud": settings.jwt_audience,
    }
    return IssuedToken(
        encoded=jwt.encode(payload, settings.jwt_secret, algorithm="HS256"),
        claims=claims,
    )


def decode_token(
    token: str,
    settings: Settings,
    *,
    expected_type: TokenType,
) -> TokenClaims:
    try:
        payload = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=["HS256"],
            audience=settings.jwt_audience,
            issuer=settings.jwt_issuer,
            options={
                "require": ["sub", "role", "type", "iat", "exp", "jti", "iss", "aud"]
            },
        )
    except ExpiredSignatureError as exc:
        raise TokenExpiredError("token expired") from exc
    except InvalidTokenError as exc:
        raise TokenValidationError("token invalid") from exc

    role = payload.get("role")
    token_type = payload.get("type")
    if role not in {"admin", "viewer"} or token_type != expected_type:
        raise TokenValidationError("token claims invalid")

    try:
        return TokenClaims(
            sub=str(payload["sub"]),
            role=role,
            token_type=token_type,
            iat=int(payload["iat"]),
            exp=int(payload["exp"]),
            jti=str(payload["jti"]),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise TokenValidationError("token claims invalid") from exc


def utc_iso_from_epoch(epoch: int) -> str:
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
