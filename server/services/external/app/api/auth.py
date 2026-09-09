# 사용자의 로그인·토큰 갱신·로그아웃을 처리하고 토큰 이력은 Data에 저장한다.
from __future__ import annotations

import hashlib
import hmac
import uuid
from typing import Any

from fastapi import (
    APIRouter,
    Body,
    Depends,
    HTTPException,
    Request,
    Response,
    status,
)
from fastapi.responses import JSONResponse

from ..clients.data import (
    DataClient,
    DataConflict,
    DataNotFound,
    DataServiceError,
)
from ..config import Settings
from ..dependencies import (
    get_data_client,
    get_login_backoff,
    get_settings_dependency,
)
from ..schemas import (
    LoginRequest,
    LogoutRequest,
    RefreshRequest,
    TokenResponse,
)
from ..security.login_backoff import LoginBackoff
from ..security.client_address import TrustedProxyAddresses
from ..security.passwords import verify_password
from ..security.tokens import (
    TokenExpiredError,
    TokenValidationError,
    decode_token,
    issue_token,
    utc_iso_from_epoch,
)
from .representations import _public_user

router = APIRouter()


# 인증 실패 응답에 Bearer challenge를 넣어 클라이언트가 재인증 필요를 판단하게 한다.
def _auth_error(detail: str = "Invalid credentials") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


# Data 응답의 현재 ID와 이전 user_id 표현을 수용하되 식별자가 없으면 응답 오류로 처리한다.
def _user_id(user: dict[str, Any]) -> str:
    value = user.get("id", user.get("user_id"))
    if value is None:
        raise DataServiceError("invalid user response")
    return str(value)


# Data가 돌려준 역할도 다시 검사하여 알려지지 않은 역할로 토큰을 발급하지 않는다.
def _user_role(user: dict[str, Any]) -> str:
    role = user.get("role")
    if role not in {"admin", "viewer"}:
        raise DataServiceError("invalid user response")
    return str(role)


def _user_is_active(user: dict[str, Any]) -> bool:
    return bool(user.get("is_active", user.get("active", True)))


# 접근·갱신 토큰은 같은 사용자 역할로 발급하되 용도와 유효 기간을 구분한다.
def _issue_pair(
    settings: Settings,
    user_id: str,
    role: str,
    *,
    family_id: str | None = None,
) -> tuple[Any, Any]:
    family_id = family_id or uuid.uuid4().hex
    access = issue_token(
        settings,
        user_id=user_id,
        role=role,
        token_type="access",
        ttl_seconds=settings.access_ttl_seconds,
        session_id=family_id,
    )
    refresh = issue_token(
        settings,
        user_id=user_id,
        role=role,
        token_type="refresh",
        ttl_seconds=settings.refresh_ttl_seconds,
        session_id=family_id,
    )
    return access, refresh


def _token_hash(encoded: str) -> str:
    # DB에는 토큰 원문 대신 지문을 남겨, 제출된 토큰과 일치하는지만 비교한다.
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# 토큰 해시와 로그인 계열을 저장하고 교체 시 이전 jti를 함께 전달한다.
def _refresh_record(
    token: Any,
    *,
    family_id: str | None = None,
    rotated_from_jti: str | None = None,
) -> dict[str, Any]:
    record = {
        "jti": token.claims.jti,
        "user_id": token.claims.sub,
        "token_hash": _token_hash(token.encoded),
        "family_id": family_id or token.claims.session_id or token.claims.jti,
        "expires_at": utc_iso_from_epoch(token.claims.exp),
    }
    if rotated_from_jti is not None:
        record["rotated_from_jti"] = rotated_from_jti
    return record


# 접근 쿠키는 전체 경로에, 갱신 쿠키는 인증 경로에만 보내도록 범위를 구분한다.
def _set_auth_cookies(
    response: Response, settings: Settings, access: Any, refresh: Any
) -> None:
    response.set_cookie(
        key=settings.access_cookie_name,
        value=access.encoded,
        max_age=settings.access_ttl_seconds,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite=settings.cookie_samesite,
    )
    response.set_cookie(
        key=settings.refresh_cookie_name,
        value=refresh.encoded,
        max_age=settings.refresh_ttl_seconds,
        path="/api/v1/auth",
        secure=settings.cookie_secure,
        httponly=True,
        samesite=settings.cookie_samesite,
    )


# 발급 때와 같은 이름·경로·속성을 사용해야 브라우저의 두 쿠키가 제거된다.
def _clear_auth_cookies(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        settings.access_cookie_name,
        path="/",
        secure=settings.cookie_secure,
        httponly=True,
        samesite=settings.cookie_samesite,
    )
    response.delete_cookie(
        settings.refresh_cookie_name,
        path="/api/v1/auth",
        secure=settings.cookie_secure,
        httponly=True,
        samesite=settings.cookie_samesite,
    )


# 모바일용 JSON과 브라우저용 HttpOnly 쿠키에 같은 토큰 쌍을 제공한다.
def _token_response(
    settings: Settings, access: Any, refresh: Any, user: dict[str, Any]
) -> JSONResponse:
    response = JSONResponse(
        {
            "access_token": access.encoded,
            "refresh_token": refresh.encoded,
            "token_type": "bearer",
            "expires_in": settings.access_ttl_seconds,
            "user": _public_user(user),
        }
    )
    _set_auth_cookies(response, settings, access, refresh)
    return response


# 유효한 Bearer 형식의 비어 있지 않은 토큰만 골라 로그아웃 처리에 사용한다.
def _extract_bearer(request: Request) -> str | None:
    authorization = request.headers.get("Authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return None


# 검증한 접속 주소를 계정별 지연과 주소별 실패 제한 양쪽에 사용한다.
async def _login_address(request: Request, settings: Settings) -> str:
    resolver = getattr(request.app.state, "trusted_proxy_addresses", None)
    if resolver is None:
        resolver = TrustedProxyAddresses(settings.trusted_proxy_hosts)
        request.app.state.trusted_proxy_addresses = resolver
    return await resolver.client_address(request)


# 모바일이 본문에 준 갱신 토큰을 우선하고 없을 때 브라우저 쿠키를 사용한다.
def _optional_refresh_token(
    request: Request,
    settings: Settings,
    body_token: Any,
) -> str | None:
    if body_token is not None:
        return body_token.get_secret_value()
    return request.cookies.get(settings.refresh_cookie_name)


# 실패 지연을 적용하고 비밀번호·활성 계정 확인 및 세션 저장이 끝난 뒤 토큰을 돌려준다.
@router.post("/api/v1/auth/login", response_model=TokenResponse)
async def login(
    payload: LoginRequest,
    request: Request,
    settings: Settings = Depends(get_settings_dependency),
    data: DataClient = Depends(get_data_client),
    backoff: LoginBackoff = Depends(get_login_backoff),
) -> Response:
    address = await _login_address(request, settings)
    key = f"{address}/{payload.username.casefold()}"
    retry_after = backoff.retry_after(key, address=address)
    if retry_after:
        raise HTTPException(
            status_code=429,
            detail="Login temporarily delayed",
            headers={"Retry-After": str(retry_after)},
        )

    try:
        user = await data.get_user_by_username(payload.username)
    except DataNotFound:
        backoff.record_failure(key, address=address)
        raise _auth_error()

    password_hash = user.get("password_hash")
    password_valid = isinstance(password_hash, str) and verify_password(
        password_hash,
        payload.password.get_secret_value(),
    )
    if not password_valid or not _user_is_active(user):
        backoff.record_failure(key, address=address)
        raise _auth_error()

    user_id = _user_id(user)
    role = _user_role(user)
    access, refresh = _issue_pair(settings, user_id, role)
    await data.create_refresh_token(_refresh_record(refresh))
    backoff.clear(key)
    return _token_response(settings, access, refresh, user)


# 서명만 믿지 않고 DB의 소유자·해시·교체 상태를 확인한 후 최신 역할로 새 토큰을 발급한다.
@router.post("/api/v1/auth/refresh", response_model=TokenResponse)
async def refresh(
    request: Request,
    payload: RefreshRequest | None = Body(default=None),
    settings: Settings = Depends(get_settings_dependency),
    data: DataClient = Depends(get_data_client),
) -> Response:
    body_token = payload.refresh_token if payload is not None else None
    encoded = _optional_refresh_token(request, settings, body_token)
    if not encoded:
        raise _auth_error("Refresh token required")

    try:
        claims = decode_token(encoded, settings, expected_type="refresh")
    except TokenExpiredError as exc:
        raise _auth_error("Refresh token expired") from exc
    except TokenValidationError as exc:
        raise _auth_error("Invalid refresh token") from exc

    try:
        record = await data.get_refresh_token(claims.jti)
    except DataNotFound as exc:
        raise _auth_error("Invalid refresh token") from exc
    if (
        bool(record.get("revoked", record.get("consumed", False)))
        or record.get("revoked_at") is not None
        or record.get("replaced_by_jti") is not None
    ):
        raise _auth_error("Refresh token revoked")
    record_user_id = str(record.get("user_id", claims.sub))
    if record_user_id != claims.sub:
        raise _auth_error("Invalid refresh token")
    stored_hash = record.get("token_hash")
    if not isinstance(stored_hash, str) or not hmac.compare_digest(
        stored_hash.encode("ascii", errors="ignore"),
        _token_hash(encoded).encode("ascii"),
    ):
        raise _auth_error("Invalid refresh token")

    try:
        user = await data.get_user(claims.sub)
    except DataNotFound as exc:
        raise _auth_error("User is unavailable") from exc
    if not _user_is_active(user):
        raise _auth_error("User is inactive")

    user_id = _user_id(user)
    role = _user_role(user)
    family_id = str(record.get("family_id") or claims.jti)
    if claims.session_id is not None and claims.session_id != family_id:
        raise _auth_error("Invalid refresh session")
    access, new_refresh = _issue_pair(settings, user_id, role, family_id=family_id)
    # 토큰을 갱신할 때마다 교체하되 로그인 계열은 유지한다. 교체 원자성은 Data가 보장한다.
    try:
        await data.rotate_refresh_token(
            claims.jti,
            _refresh_record(
                new_refresh,
                family_id=family_id,
                rotated_from_jti=claims.jti,
            ),
        )
    except (DataConflict, DataNotFound) as exc:
        raise _auth_error("Refresh session revoked or already rotated") from exc
    return _token_response(settings, access, new_refresh, user)


# 유효한 접근 토큰은 폐기 목록에 기록하고 갱신 세션을 제거한 다음 브라우저 쿠키를 지운다.
@router.post("/api/v1/auth/logout", status_code=204)
async def logout(
    request: Request,
    payload: LogoutRequest | None = Body(default=None),
    settings: Settings = Depends(get_settings_dependency),
    data: DataClient = Depends(get_data_client),
) -> Response:
    access_encoded = _extract_bearer(request) or request.cookies.get(
        settings.access_cookie_name
    )
    if access_encoded:
        try:
            access = decode_token(access_encoded, settings, expected_type="access")
        except TokenValidationError:
            access = None
        if access is not None:
            await data.revoke_session_family(access.session_id, access.sub)
            await data.revoke_access_token(
                access.jti,
                {
                    "user_id": access.sub,
                    "expires_at": utc_iso_from_epoch(access.exp),
                    "reason": "logout",
                },
            )

    body_token = payload.refresh_token if payload is not None else None
    refresh_encoded = _optional_refresh_token(request, settings, body_token)
    if refresh_encoded:
        try:
            refresh_claims = decode_token(
                refresh_encoded,
                settings,
                expected_type="refresh",
            )
        except TokenValidationError:
            refresh_claims = None
        if refresh_claims is not None:
            try:
                await data.revoke_refresh_token(refresh_claims.jti)
            except DataNotFound:
                pass

    response = Response(status_code=204)
    _clear_auth_cookies(response, settings)
    return response
