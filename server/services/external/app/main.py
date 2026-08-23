from __future__ import annotations

import asyncio
import hmac
import hashlib
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, AsyncIterator
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

from fastapi import (
    Body,
    Depends,
    FastAPI,
    HTTPException,
    Query,
    Request,
    Response,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, StreamingResponse

from .config import CAMERA_ID_PATTERN, Settings
from .data_client import (
    DataClient,
    DataConflict,
    DataForbidden,
    DataNotFound,
    DataServiceError,
)
from .edge_client import EdgeControlError, EdgeHttpClient
from .media_client import MediaControlError, MediaMtxClient
from .status_collector import StatusCollector
from .dependencies import (
    Principal,
    get_current_principal,
    get_data_client,
    get_login_backoff,
    get_settings_dependency,
    require_admin,
)
from .schemas import (
    AuthVerifyRequest,
    CameraCreate,
    CameraLiveResponse,
    CameraPageResponse,
    CameraPatch,
    CameraPermissionListResponse,
    CameraPermissions,
    CameraResponse,
    CameraStatusResponse,
    EventPageResponse,
    EventResponse,
    LoginRequest,
    LogoutRequest,
    MediaAuthRequest,
    RefreshRequest,
    RecordingPlaybackResponse,
    RecordingPageResponse,
    RecordingResponse,
    RecoveryJobPageResponse,
    SystemStatusResponse,
    UserCreate,
    UserPageResponse,
    UserPatch,
    UserResponse,
    VideoProfilePatch,
    VideoProfileResponse,
    TokenResponse,
)
from .security import (
    LoginBackoff,
    TokenExpiredError,
    TokenValidationError,
    decode_token,
    hash_password,
    issue_token,
    utc_iso_from_epoch,
    verify_password,
)


SERVICE_VERSION = "0.3.0"
RESOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
DUMMY_MEDIA_PASSWORD_HASH = hash_password("invalid-media-credential")


class _MediaAuthResponse(Response):
    """Release a camera lock only after MediaMTX receives the auth response."""

    def __init__(self, lock: asyncio.Lock) -> None:
        super().__init__(status_code=204)
        self._camera_lock = lock

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            self._camera_lock.release()


async def _disconnect_camera_publisher(settings: Settings, camera_id: str) -> bool:
    client = MediaMtxClient(
        settings.media_control_url,
        timeout_seconds=settings.media_control_timeout_seconds,
    )
    try:
        return await client.disconnect_publisher(camera_id)
    finally:
        await client.close()


def _auth_error(detail: str = "Invalid credentials") -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _user_id(user: dict[str, Any]) -> str:
    value = user.get("id", user.get("user_id"))
    if value is None:
        raise DataServiceError("invalid user response")
    return str(value)


def _user_role(user: dict[str, Any]) -> str:
    role = user.get("role")
    if role not in {"admin", "viewer"}:
        raise DataServiceError("invalid user response")
    return str(role)


def _user_is_active(user: dict[str, Any]) -> bool:
    return bool(user.get("is_active", user.get("active", True)))


def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "id",
        "user_id",
        "username",
        "role",
        "is_active",
        "active",
        "created_at",
        "updated_at",
    }
    return {key: value for key, value in user.items() if key in allowed}


def _public_users(payload: Any) -> Any:
    if isinstance(payload, list):
        return [_public_user(item) for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        result = dict(payload)
        result["items"] = [
            _public_user(item) for item in payload["items"] if isinstance(item, dict)
        ]
        return result
    if isinstance(payload, dict):
        return _public_user(payload)
    raise DataServiceError("invalid user response")


def _public_camera(camera: dict[str, Any]) -> dict[str, Any]:
    result = dict(camera)
    for internal_field in (
        "source_url",
        "edge_device_id",
        "edge_management_url",
        "edge_recovery_url",
        "edge_auth_token",
        "management_url",
        "recovery_url",
        "auth_token",
        # cameras.status is a schema-v1 compatibility column. Runtime state is
        # served exclusively by /cameras/{id}/status.
        "status",
    ):
        result.pop(internal_field, None)
    return result


def _public_cameras(payload: Any) -> Any:
    if isinstance(payload, list):
        return [_public_camera(item) for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        result = dict(payload)
        result["items"] = [
            _public_camera(item) for item in payload["items"] if isinstance(item, dict)
        ]
        return result
    if isinstance(payload, dict):
        return _public_camera(payload)
    raise DataServiceError("invalid camera response")


def _issue_pair(settings: Settings, user_id: str, role: str) -> tuple[Any, Any]:
    access = issue_token(
        settings,
        user_id=user_id,
        role=role,
        token_type="access",
        ttl_seconds=settings.access_ttl_seconds,
    )
    refresh = issue_token(
        settings,
        user_id=user_id,
        role=role,
        token_type="refresh",
        ttl_seconds=settings.refresh_ttl_seconds,
    )
    return access, refresh


def _token_hash(encoded: str) -> str:
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


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
        "family_id": family_id or token.claims.jti,
        "expires_at": utc_iso_from_epoch(token.claims.exp),
    }
    if rotated_from_jti is not None:
        record["rotated_from_jti"] = rotated_from_jti
    return record


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


def _extract_bearer(request: Request) -> str | None:
    authorization = request.headers.get("Authorization", "")
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    return None


def _validate_resource_id(value: str) -> str:
    if not RESOURCE_ID_PATTERN.fullmatch(value):
        raise HTTPException(status_code=400, detail="Invalid resource ID")
    return value


def _normalize_time(value: datetime | None, name: str) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise HTTPException(status_code=400, detail=f"{name} must include a timezone")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _validated_time_range(
    start: datetime | None,
    end: datetime | None,
) -> tuple[str | None, str | None]:
    if start is not None and end is not None and start >= end:
        raise HTTPException(status_code=400, detail="start must be earlier than end")
    return _normalize_time(start, "start"), _normalize_time(end, "end")


def _validate_source_url(value: str | None) -> None:
    if value is None:
        return
    parsed = urlsplit(value)
    if parsed.scheme not in {"rtsp", "rtsps"} or not parsed.hostname:
        raise HTTPException(
            status_code=400, detail="source_url must be a valid RTSP URL"
        )
    if any(ord(character) < 32 for character in value):
        raise HTTPException(status_code=400, detail="source_url is invalid")


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


async def _ensure_camera_access(
    data: DataClient,
    principal: Principal,
    camera_id: str,
) -> dict[str, Any]:
    if not CAMERA_ID_PATTERN.fullmatch(camera_id):
        raise HTTPException(status_code=400, detail="Invalid camera ID")
    if principal.role != "admin":
        permissions = await data.get_camera_permissions(principal.user_id)
        if camera_id not in _camera_ids_from_permissions(permissions):
            raise DataForbidden("camera access denied")
    return await data.get_camera(camera_id, user_id=principal.user_id)


def _playback_url(settings: Settings, segment: dict[str, Any]) -> str:
    camera_id = str(segment.get("camera_id", ""))
    if not CAMERA_ID_PATTERN.fullmatch(camera_id):
        raise DataServiceError("invalid recording response")
    try:
        start = datetime.fromisoformat(
            str(segment["start_time"]).replace("Z", "+00:00")
        )
        end = datetime.fromisoformat(str(segment["end_time"]).replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError) as exc:
        raise DataServiceError("invalid recording response") from exc
    if start.tzinfo is None or end.tzinfo is None or end <= start:
        raise DataServiceError("invalid recording response")
    duration = (end - start).total_seconds()
    query = urlencode(
        {
            "path": camera_id,
            "start": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "duration": f"{duration:.3f}",
            "format": "fmp4",
        }
    )
    path = f"{settings.public_playback_prefix}/get?{query}"
    return f"{settings.public_base_url}{path}" if settings.public_base_url else path


def _public_media_url(settings: Settings, path: str) -> str:
    return f"{settings.public_base_url}{path}" if settings.public_base_url else path


def _validate_edge_url(value: str | None, name: str) -> None:
    if value is None:
        return
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(part == ".." for part in parsed.path.split("/"))
    ):
        raise HTTPException(
            status_code=400, detail=f"{name} must be a credential-free HTTP(S) URL"
        )


def _edge_capability_error(
    capabilities: dict[str, Any],
) -> EdgeControlError | None:
    capability_status = capabilities.get("capability_status")
    if capability_status is not None and capability_status not in {
        "available",
        "unavailable",
        "unknown",
    }:
        return EdgeControlError(
            "INVALID_EDGE_RESPONSE",
            "The Edge device returned an invalid capability status.",
        )

    for field in ("camera_available", "encoder_available"):
        value = capabilities.get(field)
        if (
            field in capabilities
            and value is not True
            and value is not False
            and value is not None
        ):
            return EdgeControlError(
                "INVALID_EDGE_RESPONSE",
                f"The Edge device returned an invalid {field} flag.",
            )

    details = {
        field: capabilities.get(field)
        for field in (
            "capability_status",
            "camera_available",
            "encoder_available",
        )
        if field in capabilities
    }
    if capabilities.get("camera_available") is False:
        return EdgeControlError(
            "CAMERA_UNAVAILABLE",
            "The Edge camera input is unavailable.",
            status_code=409,
            details=details,
        )
    if capabilities.get("encoder_available") is False:
        return EdgeControlError(
            "ENCODER_UNAVAILABLE",
            "The Edge video encoder is unavailable.",
            status_code=409,
            details=details,
        )

    explicit_unknown_flag = any(
        field in capabilities and capabilities[field] is None
        for field in ("camera_available", "encoder_available")
    )
    if capability_status in {"unknown", "unavailable"} or explicit_unknown_flag:
        return EdgeControlError(
            "CAPABILITY_UNKNOWN",
            "The Edge camera or encoder capability could not be verified.",
            status_code=409,
            details=details,
        )
    return None


def _login_key(request: Request, username: str) -> str:
    host = request.client.host if request.client is not None else "unknown"
    return f"{host}:{username.casefold()}"


def _optional_refresh_token(
    request: Request,
    settings: Settings,
    body_token: Any,
) -> str | None:
    if body_token is not None:
        return body_token.get_secret_value()
    return request.cookies.get(settings.refresh_cookie_name)


@asynccontextmanager
async def _lifespan(application: FastAPI):
    collector_task: asyncio.Task[None] | None = None
    owns_client = False
    settings = getattr(application.state, "runtime_settings", None)
    if settings is None:
        try:
            settings = Settings.from_env()
        except RuntimeError:
            # Unit tests commonly override FastAPI dependencies after app
            # construction and intentionally do not install deployment secrets.
            settings = None
    client = getattr(application.state, "data_client", None)
    if settings is not None and client is None:
        client = DataClient(
            base_url=settings.data_base_url,
            health_url=settings.data_health_url,
            internal_token=settings.internal_token,
        )
        application.state.data_client = client
        owns_client = True
    if settings is not None and client is not None:
        collector = StatusCollector(
            settings=settings,
            data_client=client,
            camera_lock_factory=getattr(
                application.state, "camera_lifecycle_lock_factory", None
            ),
        )
        collector_task = asyncio.create_task(
            collector.run(), name="external-edge-status-collector"
        )
    try:
        yield
    finally:
        if collector_task is not None:
            collector_task.cancel()
            await asyncio.gather(collector_task, return_exceptions=True)
        if client is not None and (owns_client or hasattr(client, "close")):
            await client.close()


def create_app(
    settings: Settings | None = None,
    data_client: DataClient | None = None,
) -> FastAPI:
    application = FastAPI(
        title="AI CCTV External Service",
        version=SERVICE_VERSION,
        lifespan=_lifespan,
        openapi_url="/api/v1/openapi.json",
        docs_url="/api/v1/docs",
        redoc_url=None,
    )
    if settings is not None:
        application.state.runtime_settings = settings
    if data_client is not None:
        application.state.data_client = data_client
    # MediaMTX can ask about attacker-selected RTSP paths before credentials are
    # accepted. A fixed pool keeps those unauthenticated names from growing an
    # unbounded per-ID lock registry; collisions only add safe serialization.
    camera_lifecycle_locks = tuple(asyncio.Lock() for _ in range(64))

    def camera_lifecycle_lock(camera_id: str) -> asyncio.Lock:
        digest = hashlib.sha256(camera_id.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:8], "big") % len(camera_lifecycle_locks)
        return camera_lifecycle_locks[bucket]

    application.state.camera_lifecycle_lock_factory = camera_lifecycle_lock

    async def hold_camera_lifecycle_lock(camera_id: str) -> AsyncIterator[None]:
        async with camera_lifecycle_lock(camera_id):
            yield

    @application.exception_handler(DataServiceError)
    async def handle_data_error(_: Request, exc: DataServiceError) -> JSONResponse:
        if exc.code in {"CAMERA_HAS_HISTORY", "CAMERA_LIMIT_REACHED"}:
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "error": {
                        "code": exc.code,
                        "message": exc.message,
                        "details": {},
                    }
                },
            )
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": "Data service request failed"},
        )

    @application.exception_handler(EdgeControlError)
    async def handle_edge_error(_: Request, exc: EdgeControlError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": exc.details,
                }
            },
        )

    @application.exception_handler(MediaControlError)
    async def handle_media_error(_: Request, exc: MediaControlError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "details": {},
                }
            },
        )

    @application.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _: Request, exc: RequestValidationError
    ) -> JSONResponse:
        safe_errors = []
        for error in exc.errors():
            safe_errors.append(
                {
                    "type": error.get("type", "validation_error"),
                    "loc": error.get("loc", ()),
                    "msg": error.get("msg", "Invalid request"),
                }
            )
        return JSONResponse(status_code=422, content={"detail": safe_errors})

    @application.exception_handler(RuntimeError)
    async def handle_configuration_error(_: Request, __: RuntimeError) -> JSONResponse:
        return JSONResponse(
            status_code=503,
            content={"detail": "Service configuration is unavailable"},
        )

    @application.get("/health/live", include_in_schema=False)
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/health/ready", include_in_schema=False)
    async def health_ready(
        data: DataClient = Depends(get_data_client),
    ) -> dict[str, Any]:
        await data.health()
        return {"status": "ready"}

    @application.post("/api/v1/auth/login", response_model=TokenResponse)
    async def login(
        payload: LoginRequest,
        request: Request,
        settings: Settings = Depends(get_settings_dependency),
        data: DataClient = Depends(get_data_client),
        backoff: LoginBackoff = Depends(get_login_backoff),
    ) -> Response:
        key = _login_key(request, payload.username)
        retry_after = backoff.retry_after(key)
        if retry_after:
            raise HTTPException(
                status_code=429,
                detail="Login temporarily delayed",
                headers={"Retry-After": str(retry_after)},
            )

        try:
            user = await data.get_user_by_username(payload.username)
        except DataNotFound:
            backoff.record_failure(key)
            raise _auth_error()

        password_hash = user.get("password_hash")
        password_valid = isinstance(password_hash, str) and verify_password(
            password_hash,
            payload.password.get_secret_value(),
        )
        if not password_valid or not _user_is_active(user):
            backoff.record_failure(key)
            raise _auth_error()

        user_id = _user_id(user)
        role = _user_role(user)
        access, refresh = _issue_pair(settings, user_id, role)
        await data.create_refresh_token(
            _refresh_record(refresh, family_id=refresh.claims.jti)
        )
        backoff.clear(key)
        return _token_response(settings, access, refresh, user)

    @application.post("/api/v1/auth/refresh", response_model=TokenResponse)
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
        access, new_refresh = _issue_pair(settings, user_id, role)
        family_id = str(record.get("family_id") or claims.jti)
        await data.rotate_refresh_token(
            claims.jti,
            _refresh_record(
                new_refresh,
                family_id=family_id,
                rotated_from_jti=claims.jti,
            ),
        )
        return _token_response(settings, access, new_refresh, user)

    @application.post("/api/v1/auth/logout", status_code=204)
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

    @application.get("/api/v1/cameras", response_model=CameraPageResponse)
    async def list_cameras(
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        principal: Principal = Depends(get_current_principal),
        data: DataClient = Depends(get_data_client),
    ) -> Any:
        return _public_cameras(
            await data.list_cameras(
                user_id=principal.user_id,
                limit=limit,
                offset=offset,
            )
        )

    @application.post("/api/v1/cameras", status_code=201, response_model=CameraResponse)
    async def create_camera(
        payload: CameraCreate,
        response: Response,
        _: Principal = Depends(require_admin),
        settings: Settings = Depends(get_settings_dependency),
        data: DataClient = Depends(get_data_client),
    ) -> Any:
        _validate_source_url(payload.source_url)
        _validate_edge_url(payload.edge_management_url, "edge_management_url")
        _validate_edge_url(payload.edge_recovery_url, "edge_recovery_url")
        if payload.stream_path is not None and payload.stream_path != payload.camera_id:
            raise HTTPException(
                status_code=400, detail="stream_path must match camera_id"
            )
        body = payload.model_dump(exclude_none=True, exclude={"edge_auth_token"})
        if payload.edge_auth_token is not None:
            body["edge_auth_token"] = payload.edge_auth_token.get_secret_value()
        body["stream_path"] = payload.camera_id
        requested_enabled = bool(body.get("enabled", True))
        # Keep admission closed until the authoritative DB credential exists.
        # Otherwise a deleted/re-registered bootstrap camera can briefly fall
        # back to its stale static credential between these two Data calls.
        create_body = {
            **body,
            "enabled": False,
            "status": "disabled",
        }
        async with camera_lifecycle_lock(payload.camera_id):
            camera = await data.create_camera(create_body)
            publish_password = secrets.token_urlsafe(32)
            try:
                await data.put_camera_publish_credential(
                    payload.camera_id,
                    {
                        "username": payload.camera_id,
                        "password_hash": hash_password(publish_password),
                    },
                )
                if requested_enabled:
                    camera = await data.update_camera(
                        payload.camera_id,
                        {"enabled": True, "status": "offline"},
                    )
            except Exception:
                # Keep the disabled row in place until any old publisher is gone;
                # deleting first would make the static bootstrap fallback valid
                # again while rollback is still in progress. If MediaMTX cannot be
                # reached, leave the fail-closed row for an administrator to retry.
                await _disconnect_camera_publisher(settings, payload.camera_id)
                await data.delete_camera(payload.camera_id)
                raise
            result = _public_camera(camera)
            result["publish_credentials"] = {
                "username": payload.camera_id,
                "password": publish_password,
            }
            response.headers["Cache-Control"] = "no-store"
            return result

    @application.patch("/api/v1/cameras/{camera_id}", response_model=CameraResponse)
    async def update_camera(
        camera_id: str,
        payload: CameraPatch,
        _: Principal = Depends(require_admin),
        settings: Settings = Depends(get_settings_dependency),
        data: DataClient = Depends(get_data_client),
    ) -> Any:
        if not CAMERA_ID_PATTERN.fullmatch(camera_id):
            raise HTTPException(status_code=400, detail="Invalid camera ID")
        body = payload.model_dump(exclude_unset=True)
        body.pop("edge_auth_token", None)
        if payload.edge_auth_token is not None:
            body["edge_auth_token"] = payload.edge_auth_token.get_secret_value()
        if not body:
            raise HTTPException(status_code=400, detail="No camera fields supplied")
        _validate_source_url(body.get("source_url"))
        _validate_edge_url(body.get("edge_management_url"), "edge_management_url")
        _validate_edge_url(body.get("edge_recovery_url"), "edge_recovery_url")
        if body.get("stream_path") is not None and body["stream_path"] != camera_id:
            raise HTTPException(
                status_code=400, detail="stream_path must match camera_id"
            )
        if "enabled" in body:
            body["status"] = "offline" if body["enabled"] else "disabled"
        async with camera_lifecycle_lock(camera_id):
            updated = await data.update_camera(camera_id, body)
            if "enabled" in body:
                await data.put_camera_runtime_status(
                    camera_id,
                    {
                        "online": False,
                        "camera_input": "unknown",
                        "central_connection_status": "unknown",
                        "last_error_code": (
                            None if body["enabled"] else "CAMERA_DISABLED"
                        ),
                    },
                )
                if not body["enabled"]:
                    # Persist disabled first so reconnect attempts fail closed. If
                    # MediaMTX is unavailable the camera remains disabled and the
                    # administrator can safely retry this idempotent request.
                    await _disconnect_camera_publisher(settings, camera_id)
            return _public_camera(updated)

    @application.delete("/api/v1/cameras/{camera_id}", status_code=204)
    async def delete_camera(
        camera_id: str,
        principal: Principal = Depends(require_admin),
        settings: Settings = Depends(get_settings_dependency),
        data: DataClient = Depends(get_data_client),
    ) -> Response:
        if not CAMERA_ID_PATTERN.fullmatch(camera_id):
            raise HTTPException(status_code=400, detail="Invalid camera ID")
        async with camera_lifecycle_lock(camera_id):
            deletion_status = await data.get_camera_deletion_status(camera_id)
            if not bool(deletion_status.get("deletable")):
                raise DataConflict(
                    "Camera history must be retained; disable the camera instead.",
                    code="CAMERA_HAS_HISTORY",
                )
            previous = await data.get_camera(camera_id, user_id=principal.user_id)
            # Disable admission before terminating the active publisher. Deletion
            # happens only after the kick succeeds, leaving a retryable disabled
            # record if MediaMTX control is temporarily unavailable.
            await data.update_camera(
                camera_id, {"enabled": False, "status": "disabled"}
            )
            await data.put_camera_runtime_status(
                camera_id,
                {
                    "online": False,
                    "camera_input": "unknown",
                    "central_connection_status": "unknown",
                    "last_error_code": "CAMERA_DISABLED",
                },
            )
            await _disconnect_camera_publisher(settings, camera_id)
            try:
                await data.delete_camera(camera_id)
            except DataConflict:
                # A recording/event can arrive between the preflight check and
                # the transactional delete. Restore admission in that rare race
                # so a history conflict never silently strands a live camera.
                was_enabled = bool(previous.get("enabled", True))
                await data.update_camera(
                    camera_id,
                    {
                        "enabled": was_enabled,
                        "status": "offline" if was_enabled else "disabled",
                    },
                )
                await data.put_camera_runtime_status(
                    camera_id,
                    {
                        "online": False,
                        "camera_input": "unknown",
                        "central_connection_status": "unknown",
                        "last_error_code": (
                            "DELETE_ABORTED_HISTORY"
                            if was_enabled
                            else "CAMERA_DISABLED"
                        ),
                    },
                )
                raise
            return Response(status_code=204)

    @application.post(
        "/api/v1/cameras/{camera_id}/publish-credentials/rotate",
        response_model=CameraResponse,
    )
    async def rotate_camera_publish_credentials(
        camera_id: str,
        response: Response,
        principal: Principal = Depends(require_admin),
        settings: Settings = Depends(get_settings_dependency),
        data: DataClient = Depends(get_data_client),
    ) -> Any:
        """Rotate one camera credential and return the plaintext exactly once."""

        if not CAMERA_ID_PATTERN.fullmatch(camera_id):
            raise HTTPException(status_code=400, detail="Invalid camera ID")
        async with camera_lifecycle_lock(camera_id):
            camera = await data.get_camera(camera_id, user_id=principal.user_id)
            was_enabled = bool(camera.get("enabled", True))

            # Block reconnects while the old publisher is terminated and its
            # database credential is replaced. Failures leave the camera disabled.
            await data.update_camera(
                camera_id, {"enabled": False, "status": "disabled"}
            )
            await data.put_camera_runtime_status(
                camera_id,
                {
                    "online": False,
                    "camera_input": "unknown",
                    "central_connection_status": "unknown",
                    "last_error_code": "PUBLISH_CREDENTIAL_ROTATING",
                },
            )
            await _disconnect_camera_publisher(settings, camera_id)

            publish_password = secrets.token_urlsafe(32)
            await data.put_camera_publish_credential(
                camera_id,
                {
                    "username": camera_id,
                    "password_hash": hash_password(publish_password),
                },
            )
            updated = await data.update_camera(
                camera_id,
                {
                    "enabled": was_enabled,
                    "status": "offline" if was_enabled else "disabled",
                },
            )
            await data.put_camera_runtime_status(
                camera_id,
                {
                    "online": False,
                    "camera_input": "unknown",
                    "central_connection_status": "unknown",
                    "last_error_code": None if was_enabled else "CAMERA_DISABLED",
                },
            )
            result = _public_camera(updated)
            result["publish_credentials"] = {
                "username": camera_id,
                "password": publish_password,
            }
            response.headers["Cache-Control"] = "no-store"
            return result

    @application.get("/api/v1/cameras/{camera_id}", response_model=CameraResponse)
    async def get_camera(
        camera_id: str,
        principal: Principal = Depends(get_current_principal),
        data: DataClient = Depends(get_data_client),
    ) -> Any:
        return _public_camera(await _ensure_camera_access(data, principal, camera_id))

    @application.get(
        "/api/v1/cameras/{camera_id}/live", response_model=CameraLiveResponse
    )
    async def get_camera_live(
        camera_id: str,
        principal: Principal = Depends(get_current_principal),
        settings: Settings = Depends(get_settings_dependency),
        data: DataClient = Depends(get_data_client),
    ) -> Any:
        camera = await _ensure_camera_access(data, principal, camera_id)
        if not bool(camera.get("enabled", True)):
            raise HTTPException(status_code=409, detail="Camera is disabled")
        relative_url = (
            f"{settings.public_hls_prefix}/{quote(camera_id, safe='')}/index.m3u8"
        )
        media_url = _public_media_url(settings, relative_url)
        return {
            "camera_id": camera_id,
            "protocol": "hls",
            "url": media_url,
            "hls_url": media_url,
            "auth": {
                "method": "cookie",
                "cookie_name": settings.access_cookie_name,
            },
        }

    @application.get(
        "/api/v1/cameras/{camera_id}/video-profile",
        response_model=VideoProfileResponse,
    )
    async def get_camera_video_profile(
        camera_id: str,
        principal: Principal = Depends(get_current_principal),
        data: DataClient = Depends(get_data_client),
    ) -> Any:
        await _ensure_camera_access(data, principal, camera_id)
        profile = await data.get_camera_video_profile(camera_id)
        return {
            key: profile.get(key)
            for key in (
                "camera_id",
                "current_profile",
                "desired_profile",
                "supported_profiles",
                "edge_online",
                "last_error_code",
            )
        }

    @application.patch(
        "/api/v1/cameras/{camera_id}/video-profile",
        response_model=VideoProfileResponse,
    )
    async def update_camera_video_profile(
        camera_id: str,
        payload: VideoProfilePatch,
        _: Principal = Depends(require_admin),
        settings: Settings = Depends(get_settings_dependency),
        data: DataClient = Depends(get_data_client),
        _lifecycle_lock: None = Depends(hold_camera_lifecycle_lock),
    ) -> Any:
        await _ensure_camera_access(data, _, camera_id)
        await data.update_camera_video_profile(
            camera_id, {"desired_profile": payload.profile}
        )
        try:
            target = await data.get_camera_control_target(camera_id)
        except DataConflict as exc:
            edge_error = EdgeControlError(
                "CAPABILITY_UNKNOWN",
                "Edge management metadata is not configured.",
                status_code=409,
            )
            await data.update_camera_video_profile(
                camera_id, {"last_error_code": edge_error.code}
            )
            await data.create_event(
                {
                    "camera_id": camera_id,
                    "event_type": "video_profile_change_failed",
                    "occurred_at": datetime.now(timezone.utc).isoformat(),
                    "metadata": {
                        "requested_profile": payload.profile,
                        "reason_code": edge_error.code,
                    },
                }
            )
            raise edge_error from exc

        edge = EdgeHttpClient(
            base_url=str(target["management_url"]),
            auth_token=str(target["auth_token"]),
            timeout_seconds=settings.edge_control_timeout_seconds,
        )
        try:
            capabilities = await edge.get_video_capabilities()
            if capabilities.get("camera_id") != camera_id:
                raise EdgeControlError(
                    "INVALID_EDGE_RESPONSE",
                    "The Edge capability camera ID did not match.",
                )
            capability_error = _edge_capability_error(capabilities)
            if capability_error is not None:
                raise capability_error
            supported = capabilities.get("supported_profiles")
            if (
                not isinstance(supported, list)
                or not supported
                or any(profile not in {"hd", "fhd"} for profile in supported)
            ):
                raise EdgeControlError(
                    "INVALID_EDGE_RESPONSE",
                    "The Edge device returned invalid video capabilities.",
                )
            await data.update_camera_video_profile(
                camera_id,
                {
                    "supported_profiles": supported,
                    "encoder": capabilities.get("encoder") or "unknown",
                },
            )
            if payload.profile not in supported:
                raise EdgeControlError(
                    "UNSUPPORTED_VIDEO_PROFILE",
                    "The Edge device does not support the requested video profile.",
                    status_code=409,
                    details={
                        "requested_profile": payload.profile,
                        "supported_profiles": supported,
                    },
                )
            applied = await edge.apply_video_profile(payload.profile)
            if applied.get("current_profile") != payload.profile:
                raise EdgeControlError(
                    "INVALID_EDGE_RESPONSE",
                    "The Edge device did not confirm the requested video profile.",
                    profile_outcome_journaled=True,
                )
        except EdgeControlError as exc:
            await data.update_camera_video_profile(
                camera_id, {"last_error_code": exc.code}
            )
            if not exc.profile_outcome_journaled:
                await data.create_event(
                    {
                        "camera_id": camera_id,
                        "event_type": "video_profile_change_failed",
                        "occurred_at": datetime.now(timezone.utc).isoformat(),
                        "metadata": {
                            "requested_profile": payload.profile,
                            "reason_code": exc.code,
                        },
                    }
                )
            raise
        finally:
            await edge.close()

        # AC-002: current_profile changes only after the Edge's explicit
        # applied response above has been validated.
        profile = await data.update_camera_video_profile(
            camera_id,
            {
                "current_profile": payload.profile,
                "supported_profiles": supported,
                "encoder": capabilities.get("encoder") or "unknown",
                "last_error_code": None,
            },
        )
        # The Data Service mirrors current_profile to camera runtime state in
        # the same transaction. Avoid a partial status write here: a successful
        # control call does not refresh CPU/power/input telemetry.
        # ProfileManager wrote the authoritative success event before it sent
        # the applied response. The Status Collector imports that durable Edge
        # journal entry; writing another event here would duplicate it.
        return {
            key: profile.get(key)
            for key in (
                "camera_id",
                "current_profile",
                "desired_profile",
                "supported_profiles",
                "edge_online",
                "last_error_code",
            )
        }

    @application.get(
        "/api/v1/cameras/{camera_id}/status",
        response_model=CameraStatusResponse,
    )
    async def get_camera_status(
        camera_id: str,
        principal: Principal = Depends(get_current_principal),
        data: DataClient = Depends(get_data_client),
    ) -> Any:
        await _ensure_camera_access(data, principal, camera_id)
        runtime = await data.get_camera_runtime_status(camera_id)
        allowed = (
            "camera_id",
            "online",
            "cpu_percent",
            "memory_percent",
            "storage_percent",
            "battery_percent",
            "power_source",
            "camera_input",
            "central_connection_status",
            "current_video_profile",
            "last_seen_at",
            "last_error_code",
        )
        return {key: runtime.get(key) for key in allowed}

    @application.get("/api/v1/recordings", response_model=RecordingPageResponse)
    async def list_recordings(
        camera_id: str = Query(),
        start: datetime = Query(alias="from"),
        end: datetime = Query(alias="to"),
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        principal: Principal = Depends(get_current_principal),
        data: DataClient = Depends(get_data_client),
    ) -> Any:
        await _ensure_camera_access(data, principal, camera_id)
        start_utc, end_utc = _validated_time_range(start, end)
        return await data.list_recordings(
            user_id=principal.user_id,
            camera_id=camera_id,
            start=start_utc,
            end=end_utc,
            limit=limit,
            offset=offset,
        )

    @application.get(
        "/api/v1/recordings/{segment_id}", response_model=RecordingResponse
    )
    async def get_recording(
        segment_id: str,
        principal: Principal = Depends(get_current_principal),
        data: DataClient = Depends(get_data_client),
    ) -> Any:
        recording = await data.get_recording(
            _validate_resource_id(segment_id),
            user_id=principal.user_id,
        )
        await _ensure_camera_access(
            data, principal, str(recording.get("camera_id", ""))
        )
        return recording

    @application.get(
        "/api/v1/recordings/{segment_id}/playback",
        response_model=RecordingPlaybackResponse,
    )
    async def get_recording_playback(
        segment_id: str,
        principal: Principal = Depends(get_current_principal),
        settings: Settings = Depends(get_settings_dependency),
        data: DataClient = Depends(get_data_client),
    ) -> Any:
        segment_id = _validate_resource_id(segment_id)
        recording = await data.get_recording(segment_id, user_id=principal.user_id)
        await _ensure_camera_access(
            data, principal, str(recording.get("camera_id", ""))
        )
        if recording.get("format") == "mpegts":
            relative_url = f"/api/v1/recordings/{quote(segment_id, safe='')}/content"
            playback_url = _public_media_url(settings, relative_url)
        else:
            playback_url = _playback_url(settings, recording)
        return {
            "recording_id": segment_id,
            "playback_url": playback_url,
        }

    @application.get(
        "/api/v1/recordings/{segment_id}/content",
        response_class=StreamingResponse,
        responses={
            200: {
                "description": "Complete recording content",
                "content": {
                    "video/mp2t": {"schema": {"type": "string", "format": "binary"}},
                    "video/mp4": {"schema": {"type": "string", "format": "binary"}},
                },
            },
            206: {
                "description": "Recording byte range",
                "content": {
                    "video/mp2t": {"schema": {"type": "string", "format": "binary"}},
                    "video/mp4": {"schema": {"type": "string", "format": "binary"}},
                },
            },
            416: {"description": "Unsatisfiable byte range"},
        },
    )
    async def get_recording_content(
        segment_id: str,
        request: Request,
        principal: Principal = Depends(get_current_principal),
        data: DataClient = Depends(get_data_client),
    ) -> StreamingResponse:
        segment_id = _validate_resource_id(segment_id)
        recording = await data.get_recording(segment_id, user_id=principal.user_id)
        await _ensure_camera_access(
            data, principal, str(recording.get("camera_id", ""))
        )
        upstream = await data.open_recording_content(
            segment_id,
            range_header=request.headers.get("range"),
            if_range_header=request.headers.get("if-range"),
        )
        forwarded_headers = {
            name: value
            for name, value in upstream.headers.items()
            if name.lower()
            in {
                "accept-ranges",
                "cache-control",
                "content-length",
                "content-range",
                "etag",
                "last-modified",
            }
        }

        async def chunks() -> AsyncIterator[bytes]:
            try:
                if upstream.is_stream_consumed:
                    yield upstream.content
                else:
                    async for chunk in upstream.aiter_raw():
                        yield chunk
            finally:
                await upstream.aclose()

        return StreamingResponse(
            chunks(),
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type", "application/octet-stream"),
            headers=forwarded_headers,
        )

    @application.get("/api/v1/events", response_model=EventPageResponse)
    async def list_events(
        camera_id: str | None = Query(default=None),
        event_type: str | None = Query(default=None, min_length=1, max_length=128),
        start: datetime | None = Query(default=None, alias="from"),
        end: datetime | None = Query(default=None, alias="to"),
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        principal: Principal = Depends(get_current_principal),
        data: DataClient = Depends(get_data_client),
    ) -> Any:
        if camera_id is not None and not CAMERA_ID_PATTERN.fullmatch(camera_id):
            raise HTTPException(status_code=400, detail="Invalid camera ID")
        if principal.role != "admin" and camera_id is None:
            raise HTTPException(
                status_code=400,
                detail="camera_id is required for viewer event searches",
            )
        if camera_id is not None:
            await _ensure_camera_access(data, principal, camera_id)
        start_utc, end_utc = _validated_time_range(start, end)
        return await data.list_events(
            user_id=principal.user_id,
            camera_id=camera_id,
            event_type=event_type,
            start=start_utc,
            end=end_utc,
            limit=limit,
            offset=offset,
        )

    @application.get("/api/v1/events/{event_id}", response_model=EventResponse)
    async def get_event(
        event_id: str,
        principal: Principal = Depends(get_current_principal),
        data: DataClient = Depends(get_data_client),
    ) -> Any:
        event = await data.get_event(
            _validate_resource_id(event_id),
            user_id=principal.user_id,
        )
        await _ensure_camera_access(data, principal, str(event.get("camera_id", "")))
        return event

    @application.get("/api/v1/system/status", response_model=SystemStatusResponse)
    @application.get("/api/v1/admin/system/status", response_model=SystemStatusResponse)
    async def system_status(
        _: Principal = Depends(require_admin),
        data: DataClient = Depends(get_data_client),
    ) -> Any:
        data_status = await data.health()
        return {
            "external": {"status": "running", "version": SERVICE_VERSION},
            "data": data_status,
        }

    @application.get("/api/v1/recovery-jobs", response_model=RecoveryJobPageResponse)
    async def list_recovery_jobs(
        camera_id: str | None = Query(default=None),
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        _: Principal = Depends(require_admin),
        data: DataClient = Depends(get_data_client),
    ) -> Any:
        if camera_id is not None and not CAMERA_ID_PATTERN.fullmatch(camera_id):
            raise HTTPException(status_code=400, detail="Invalid camera ID")
        return await data.list_recovery_jobs(
            camera_id=camera_id, limit=limit, offset=offset
        )

    @application.get("/api/v1/admin/users", response_model=UserPageResponse)
    async def list_users(
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        _: Principal = Depends(require_admin),
        data: DataClient = Depends(get_data_client),
    ) -> Any:
        return _public_users(await data.list_users(limit=limit, offset=offset))

    @application.post(
        "/api/v1/admin/users", status_code=201, response_model=UserResponse
    )
    async def create_user(
        payload: UserCreate,
        _: Principal = Depends(require_admin),
        data: DataClient = Depends(get_data_client),
    ) -> Any:
        body = {
            "username": payload.username,
            "password_hash": hash_password(payload.password.get_secret_value()),
            "role": payload.role,
            "is_active": payload.is_active,
        }
        return _public_user(await data.create_user(body))

    @application.patch("/api/v1/admin/users/{user_id}", response_model=UserResponse)
    async def update_user(
        user_id: str,
        payload: UserPatch,
        _: Principal = Depends(require_admin),
        data: DataClient = Depends(get_data_client),
    ) -> Any:
        user_id = _validate_resource_id(user_id)
        body = payload.model_dump(exclude_unset=True, exclude={"password"})
        if payload.password is not None:
            body["password_hash"] = hash_password(payload.password.get_secret_value())
        if not body:
            raise HTTPException(status_code=400, detail="No user fields supplied")
        return _public_user(await data.update_user(user_id, body))

    @application.get(
        "/api/v1/admin/users/{user_id}/camera-permissions",
        response_model=CameraPermissionListResponse,
    )
    async def get_user_permissions(
        user_id: str,
        _: Principal = Depends(require_admin),
        data: DataClient = Depends(get_data_client),
    ) -> Any:
        return await data.get_camera_permissions(_validate_resource_id(user_id))

    @application.put(
        "/api/v1/admin/users/{user_id}/camera-permissions",
        response_model=CameraPermissionListResponse,
    )
    async def set_user_permissions(
        user_id: str,
        payload: CameraPermissions,
        _: Principal = Depends(require_admin),
        data: DataClient = Depends(get_data_client),
    ) -> Any:
        try:
            camera_ids = payload.normalized_camera_ids()
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid camera ID") from exc
        return await data.set_camera_permissions(
            _validate_resource_id(user_id),
            camera_ids,
        )

    @application.get(
        "/internal/auth/verify",
        operation_id="verify_internal_auth_get",
        include_in_schema=False,
    )
    @application.post(
        "/internal/auth/verify",
        operation_id="verify_internal_auth_post",
        include_in_schema=False,
    )
    async def internal_auth_verify(
        request: Request,
        response: Response,
        payload: AuthVerifyRequest | None = Body(default=None),
        camera_id: str | None = Query(default=None),
        principal: Principal = Depends(get_current_principal),
        data: DataClient = Depends(get_data_client),
    ) -> dict[str, bool]:
        selected_camera_id = camera_id or request.headers.get("X-Camera-ID")
        resource_type = payload.resource_type if payload is not None else None
        resource_id = payload.resource_id if payload is not None else None
        if payload is not None and payload.camera_id is not None:
            selected_camera_id = payload.camera_id

        original_uri = request.headers.get("X-Original-URI", "")
        parsed_original_uri = urlsplit(original_uri)
        original_path = parsed_original_uri.path
        decoded_original_path = unquote(original_path)
        if original_uri and (
            # Proxies and MediaMTX can normalize/decode at different layers.
            # Reject ambiguous path syntax before deciding which protected
            # prefix it represents, including encoded prefix spellings.
            "%" in original_path
            or "\\" in decoded_original_path
            or "//" in decoded_original_path
            or any(part in {".", ".."} for part in decoded_original_path.split("/"))
        ):
            raise HTTPException(status_code=400, detail="Invalid protected path")
        original_query = parse_qs(parsed_original_uri.query, keep_blank_values=True)
        hls_match = re.fullmatch(
            rf"/hls/({CAMERA_ID_PATTERN.pattern[1:-1]})(?:/.*)?",
            original_path,
        )
        if hls_match is not None:
            uri_camera_id = hls_match.group(1)
            if selected_camera_id is not None and selected_camera_id != uri_camera_id:
                raise HTTPException(
                    status_code=400, detail="Camera selector conflicts with media URI"
                )
            selected_camera_id = uri_camera_id
        if original_path.startswith("/hls/") and hls_match is None:
            raise HTTPException(status_code=400, detail="Invalid HLS path")
        if original_path in {
            "/playback/get",
            "/playback/list",
        }:
            path_values = original_query.get("path", [])
            if len(path_values) != 1:
                raise HTTPException(status_code=400, detail="Playback path is required")
            uri_camera_id = path_values[0]
            if selected_camera_id is not None and selected_camera_id != uri_camera_id:
                raise HTTPException(
                    status_code=400, detail="Camera selector conflicts with media URI"
                )
            selected_camera_id = uri_camera_id
        elif original_path.startswith("/playback/"):
            raise HTTPException(status_code=400, detail="Invalid playback path")
        elif original_uri and not original_path.startswith("/hls/"):
            # auth_request is valid only for the two protected media
            # namespaces. Never approve a raw URI that Nginx may have
            # normalized into one of them while this service saw another.
            raise HTTPException(status_code=400, detail="Invalid protected path")

        if selected_camera_id is not None:
            camera = await _ensure_camera_access(data, principal, selected_camera_id)
            if original_path.startswith("/hls/") and not bool(
                camera.get("enabled", True)
            ):
                raise DataForbidden("disabled camera has no live stream")
        elif resource_type is not None:
            if resource_id is None:
                raise HTTPException(status_code=400, detail="resource_id is required")
            resource_id = _validate_resource_id(resource_id)
            if resource_type == "camera":
                await _ensure_camera_access(data, principal, resource_id)
            elif resource_type == "recording":
                recording = await data.get_recording(
                    resource_id,
                    user_id=principal.user_id,
                )
                await _ensure_camera_access(
                    data,
                    principal,
                    str(recording.get("camera_id", "")),
                )
            elif resource_type == "event":
                event = await data.get_event(resource_id, user_id=principal.user_id)
                await _ensure_camera_access(
                    data,
                    principal,
                    str(event.get("camera_id", "")),
                )

        response.headers["X-User-ID"] = principal.user_id
        response.headers["X-User-Role"] = principal.role
        return {"valid": True}

    @application.post("/internal/media-auth", status_code=204, include_in_schema=False)
    async def internal_media_auth(
        payload: MediaAuthRequest,
        settings: Settings = Depends(get_settings_dependency),
        data: DataClient = Depends(get_data_client),
    ) -> Response:
        action = payload.action.lower()
        protocol = payload.protocol.lower()
        if action == "read" and protocol == "hls":
            # HLS is Docker-network-only and every public request has already
            # passed Nginx JWT/camera ACL auth_request.
            return Response(status_code=204)
        if action not in {"publish", "read"}:
            # Playback/API/metrics/pprof are excluded by MediaMTX config and
            # remain on the private Compose network.
            return Response(status_code=204)
        if not CAMERA_ID_PATTERN.fullmatch(payload.path):
            raise _auth_error("Media authentication failed")
        if action == "read" and protocol != "rtsp":
            raise _auth_error("Media authentication failed")

        # Hold the same lock used by lifecycle sagas from the enabled-state read
        # until the 204 has actually been sent to MediaMTX. A subsequent
        # disable/rotate/delete then closes admission before repeatedly checking
        # and kicking a publisher attaching from that immediately preceding auth.
        camera_lock = camera_lifecycle_lock(payload.path)
        await camera_lock.acquire()
        try:
            try:
                camera = await data.get_camera(payload.path, user_id="0")
            except DataNotFound as exc:
                raise _auth_error("Media authentication failed") from exc
            if not bool(camera.get("enabled", True)):
                raise _auth_error("Media authentication failed")

            if action == "read":
                username_valid = hmac.compare_digest(
                    payload.user, settings.media_read_username
                )
                password_valid = hmac.compare_digest(
                    payload.password, settings.media_read_password
                )
            else:
                # A database credential is issued on every registration/rotation
                # and is authoritative. Static credentials remain a bootstrap-only
                # fallback for cameras created before persistence existed.
                dynamic = await data.get_camera_publish_credential(payload.path)
                if dynamic is not None:
                    expected_username = str((dynamic or {}).get("username", ""))
                    password_hash = str(
                        (dynamic or {}).get("password_hash", DUMMY_MEDIA_PASSWORD_HASH)
                    )
                    username_valid = hmac.compare_digest(
                        payload.user, expected_username
                    )
                    password_valid = verify_password(password_hash, payload.password)
                else:
                    expected = settings.media_publish_credentials.get(payload.path)
                    expected_username = (
                        expected.username if expected is not None else ""
                    )
                    expected_password = (
                        expected.password if expected is not None else ""
                    )
                    username_valid = hmac.compare_digest(
                        payload.user, expected_username
                    )
                    password_valid = hmac.compare_digest(
                        payload.password, expected_password
                    )
            if not (username_valid and password_valid):
                raise _auth_error("Media authentication failed")
        except BaseException:
            camera_lock.release()
            raise
        return _MediaAuthResponse(camera_lock)

    return application


app = create_app()
