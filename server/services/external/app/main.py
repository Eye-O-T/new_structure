from __future__ import annotations

import hmac
import hashlib
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qs, quote, urlencode, urlsplit, urlunsplit

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
from fastapi.responses import JSONResponse

from .config import CAMERA_ID_PATTERN, Settings
from .data_client import (
    DataClient,
    DataForbidden,
    DataNotFound,
    DataServiceError,
)
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
    CameraPatch,
    CameraPermissions,
    LoginRequest,
    LogoutRequest,
    MediaAuthRequest,
    RefreshRequest,
    UserCreate,
    UserPatch,
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
    source_url = result.get("source_url")
    if isinstance(source_url, str):
        parsed = urlsplit(source_url)
        if parsed.username is not None or parsed.password is not None:
            try:
                port = f":{parsed.port}" if parsed.port is not None else ""
            except ValueError:
                result["source_url"] = "[redacted]"
            else:
                hostname = parsed.hostname or ""
                if ":" in hostname:
                    hostname = f"[{hostname}]"
                result["source_url"] = urlunsplit(
                    (
                        parsed.scheme,
                        f"[redacted]@{hostname}{port}",
                        parsed.path,
                        parsed.query,
                        parsed.fragment,
                    )
                )
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
    return f"{settings.public_playback_prefix}/get?{query}"


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
    yield
    client = getattr(application.state, "data_client", None)
    if client is not None:
        await client.close()


def create_app() -> FastAPI:
    application = FastAPI(
        title="AI CCTV External Service",
        version=SERVICE_VERSION,
        lifespan=_lifespan,
    )

    @application.exception_handler(DataServiceError)
    async def handle_data_error(_: Request, exc: DataServiceError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"detail": "Data service request failed"},
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

    @application.get("/health/live")
    async def health_live() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/health/ready")
    async def health_ready(
        data: DataClient = Depends(get_data_client),
    ) -> dict[str, Any]:
        await data.health()
        return {"status": "ready"}

    @application.post("/api/v1/auth/login")
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

    @application.post("/api/v1/auth/refresh")
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

    @application.get("/api/v1/cameras")
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

    @application.post("/api/v1/cameras", status_code=201)
    async def create_camera(
        payload: CameraCreate,
        response: Response,
        _: Principal = Depends(require_admin),
        data: DataClient = Depends(get_data_client),
    ) -> Any:
        _validate_source_url(payload.source_url)
        if payload.stream_path is not None and payload.stream_path != payload.camera_id:
            raise HTTPException(
                status_code=400, detail="stream_path must match camera_id"
            )
        body = payload.model_dump(exclude_none=True)
        body["stream_path"] = payload.camera_id
        camera = await data.create_camera(body)
        publish_password = secrets.token_urlsafe(32)
        try:
            await data.put_camera_publish_credential(
                payload.camera_id,
                {
                    "username": payload.camera_id,
                    "password_hash": hash_password(publish_password),
                },
            )
        except Exception:
            await data.delete_camera(payload.camera_id)
            raise
        result = _public_camera(camera)
        result["publish_credentials"] = {
            "username": payload.camera_id,
            "password": publish_password,
        }
        response.headers["Cache-Control"] = "no-store"
        return result

    @application.patch("/api/v1/cameras/{camera_id}")
    async def update_camera(
        camera_id: str,
        payload: CameraPatch,
        _: Principal = Depends(require_admin),
        data: DataClient = Depends(get_data_client),
    ) -> Any:
        if not CAMERA_ID_PATTERN.fullmatch(camera_id):
            raise HTTPException(status_code=400, detail="Invalid camera ID")
        body = payload.model_dump(exclude_unset=True)
        if not body:
            raise HTTPException(status_code=400, detail="No camera fields supplied")
        _validate_source_url(body.get("source_url"))
        if body.get("stream_path") is not None and body["stream_path"] != camera_id:
            raise HTTPException(
                status_code=400, detail="stream_path must match camera_id"
            )
        return _public_camera(await data.update_camera(camera_id, body))

    @application.delete("/api/v1/cameras/{camera_id}", status_code=204)
    async def delete_camera(
        camera_id: str,
        _: Principal = Depends(require_admin),
        data: DataClient = Depends(get_data_client),
    ) -> Response:
        if not CAMERA_ID_PATTERN.fullmatch(camera_id):
            raise HTTPException(status_code=400, detail="Invalid camera ID")
        await data.delete_camera(camera_id)
        return Response(status_code=204)

    @application.get("/api/v1/cameras/{camera_id}")
    async def get_camera(
        camera_id: str,
        principal: Principal = Depends(get_current_principal),
        data: DataClient = Depends(get_data_client),
    ) -> Any:
        return _public_camera(await _ensure_camera_access(data, principal, camera_id))

    @application.get("/api/v1/cameras/{camera_id}/live")
    async def get_camera_live(
        camera_id: str,
        principal: Principal = Depends(get_current_principal),
        settings: Settings = Depends(get_settings_dependency),
        data: DataClient = Depends(get_data_client),
    ) -> Any:
        await _ensure_camera_access(data, principal, camera_id)
        return {
            "camera_id": camera_id,
            "hls_url": f"{settings.public_hls_prefix}/{quote(camera_id, safe='')}/index.m3u8",
        }

    @application.get("/api/v1/recordings")
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

    @application.get("/api/v1/recordings/{segment_id}")
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

    @application.get("/api/v1/recordings/{segment_id}/playback")
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
        return {
            "recording_id": segment_id,
            "playback_url": _playback_url(settings, recording),
        }

    @application.get("/api/v1/events")
    async def list_events(
        camera_id: str | None = Query(default=None),
        event_type: str | None = Query(default=None, min_length=1, max_length=64),
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

    @application.get("/api/v1/events/{event_id}")
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

    @application.get("/api/v1/system/status")
    @application.get("/api/v1/admin/system/status")
    async def system_status(
        _: Principal = Depends(require_admin),
        data: DataClient = Depends(get_data_client),
    ) -> Any:
        data_status = await data.health()
        return {
            "external": {"status": "running", "version": SERVICE_VERSION},
            "data": data_status,
        }

    @application.get("/api/v1/admin/users")
    async def list_users(
        limit: int = Query(default=50, ge=1, le=100),
        offset: int = Query(default=0, ge=0),
        _: Principal = Depends(require_admin),
        data: DataClient = Depends(get_data_client),
    ) -> Any:
        return _public_users(await data.list_users(limit=limit, offset=offset))

    @application.post("/api/v1/admin/users", status_code=201)
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

    @application.patch("/api/v1/admin/users/{user_id}")
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

    @application.get("/api/v1/admin/users/{user_id}/camera-permissions")
    async def get_user_permissions(
        user_id: str,
        _: Principal = Depends(require_admin),
        data: DataClient = Depends(get_data_client),
    ) -> Any:
        return await data.get_camera_permissions(_validate_resource_id(user_id))

    @application.put("/api/v1/admin/users/{user_id}/camera-permissions")
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
    )
    @application.post(
        "/internal/auth/verify",
        operation_id="verify_internal_auth_post",
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
        original_query = parse_qs(parsed_original_uri.query, keep_blank_values=True)
        hls_match = re.fullmatch(
            rf"/hls/({CAMERA_ID_PATTERN.pattern[1:-1]})(?:/.*)?",
            original_path,
        )
        if selected_camera_id is None and hls_match is not None:
            selected_camera_id = hls_match.group(1)
        if original_path.startswith("/hls/") and hls_match is None:
            raise HTTPException(status_code=400, detail="Invalid HLS path")
        if selected_camera_id is None and original_path in {
            "/playback/get",
            "/playback/list",
        }:
            path_values = original_query.get("path", [])
            if len(path_values) != 1:
                raise HTTPException(status_code=400, detail="Playback path is required")
            selected_camera_id = path_values[0]
        elif original_path.startswith("/playback/"):
            raise HTTPException(status_code=400, detail="Invalid playback path")

        if selected_camera_id is not None:
            await _ensure_camera_access(data, principal, selected_camera_id)
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

    @application.post("/internal/media-auth", status_code=204)
    async def internal_media_auth(
        payload: MediaAuthRequest,
        settings: Settings = Depends(get_settings_dependency),
        data: DataClient = Depends(get_data_client),
    ) -> Response:
        if payload.action.lower() != "publish":
            return Response(status_code=204)
        if not CAMERA_ID_PATTERN.fullmatch(payload.path):
            raise _auth_error("Media authentication failed")
        try:
            camera = await data.get_camera(payload.path, user_id="0")
        except DataNotFound as exc:
            raise _auth_error("Media authentication failed") from exc
        if not bool(camera.get("enabled", True)):
            raise _auth_error("Media authentication failed")

        expected = settings.media_publish_credentials.get(payload.path)
        if expected is not None:
            username_valid = hmac.compare_digest(payload.user, expected.username)
            password_valid = hmac.compare_digest(payload.password, expected.password)
        else:
            dynamic = await data.get_camera_publish_credential(payload.path)
            expected_username = str((dynamic or {}).get("username", ""))
            password_hash = str(
                (dynamic or {}).get("password_hash", DUMMY_MEDIA_PASSWORD_HASH)
            )
            username_valid = hmac.compare_digest(payload.user, expected_username)
            password_valid = verify_password(password_hash, payload.password)
        if not (username_valid and password_valid):
            raise _auth_error("Media authentication failed")
        return Response(status_code=204)

    return application


app = create_app()
