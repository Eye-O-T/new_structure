# JWT의 서명·만료·용도를 검사한다. JWT 내용 자체를 암호화하는 코드는 아니다.
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal

import jwt
from jwt import ExpiredSignatureError, InvalidTokenError

from ..config import Settings

Role = Literal["admin", "viewer"]
TokenType = Literal["access", "refresh"]


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
    session_id: str | None = None


@dataclass(frozen=True)
class IssuedToken:
    encoded: str
    claims: TokenClaims


# 용도·역할·고유 jti·수신 대상이 명시된 HS256 토큰과 저장에 필요한 claims를 함께 만든다.
def issue_token(
    settings: Settings,
    *,
    user_id: str,
    role: Role,
    token_type: TokenType,
    ttl_seconds: int,
    now: datetime | None = None,
    session_id: str | None = None,
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
        session_id=session_id,
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
    if session_id is not None:
        payload["sid"] = session_id
    return IssuedToken(
        encoded=jwt.encode(payload, settings.jwt_secret, algorithm="HS256"),
        claims=claims,
    )


# 서명과 필수 claims를 검증하고 호출자가 요구한 access 또는 refresh 용도만 허용한다.
def decode_token(
    token: str,
    settings: Settings,
    *,
    expected_type: TokenType,
) -> TokenClaims:
    try:
        # 허용 알고리즘·발급자·수신 대상을 고정하여 다른 용도의 토큰을 받아들이지 않는다.
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
    session_id = payload.get("sid")
    if session_id is not None and (
        not isinstance(session_id, str)
        or not 1 <= len(session_id) <= 128
        or any(
            not (character.isascii() and (character.isalnum() or character in "_-"))
            for character in session_id
        )
    ):
        raise TokenValidationError("invalid session identity")
    if expected_type == "access" and session_id is None:
        raise TokenValidationError("session binding required; authenticate again")

    try:
        return TokenClaims(
            sub=str(payload["sub"]),
            role=role,
            token_type=token_type,
            iat=int(payload["iat"]),
            exp=int(payload["exp"]),
            jti=str(payload["jti"]),
            session_id=session_id,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise TokenValidationError("token claims invalid") from exc


# JWT의 초 단위 만료 시각을 Data에 저장할 UTC 문자열로 변환한다.
def utc_iso_from_epoch(epoch: int) -> str:
    return (
        datetime.fromtimestamp(epoch, tz=timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
