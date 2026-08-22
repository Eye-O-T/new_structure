"""FastAPI entry point for the AI_CCTV Data Service."""

from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from typing import Annotated, Any

from ai_cctv_core.identifiers import validate_camera_id
from ai_cctv_core.config import load_config
from ai_cctv_core.time import format_utc, parse_utc, utc_now
from fastapi import (
    APIRouter,
    Depends,
    FastAPI,
    Form,
    Header,
    Query,
    Request,
    Response,
    status,
)

from .config import Settings
from .database import Database
from .errors import ApiError, install_error_handlers
from .operations import (
    normalize_relative_path,
    prepare_recording_hook,
    prepare_segment,
    reconcile,
    retention_cleanup,
    storage_is_ready,
    storage_usage,
)
from .repository import DataRepository
from .schemas import (
    BackupRequest,
    CameraCreate,
    CameraPublishCredentialPut,
    CameraStatusUpdate,
    CameraUpdate,
    EventCreate,
    RecordingSegmentCreate,
    RefreshTokenCreate,
    RetentionRequest,
    RevokedTokenPut,
    UserCreate,
    UserUpdate,
)

LOGGER = logging.getLogger("ai_cctv.data")


def get_repository(request: Request) -> DataRepository:
    return request.app.state.repository


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def require_internal_token(
    request: Request,
    x_internal_token: Annotated[str | None, Header()] = None,
) -> None:
    expected = request.app.state.settings.internal_token
    if not expected:
        raise ApiError(
            503,
            "INTERNAL_TOKEN_NOT_CONFIGURED",
            "Data Service 내부 인증 토큰이 설정되지 않았습니다.",
        )
    if x_internal_token is None or not hmac.compare_digest(
        x_internal_token.encode("utf-8"), expected.encode("utf-8")
    ):
        raise ApiError(401, "INVALID_INTERNAL_TOKEN", "내부 인증에 실패했습니다.")


Repo = Annotated[DataRepository, Depends(get_repository)]
RuntimeSettings = Annotated[Settings, Depends(get_settings)]


def _not_found(resource: str) -> ApiError:
    return ApiError(
        404, f"{resource.upper()}_NOT_FOUND", "요청한 항목을 찾을 수 없습니다."
    )


def _page(items: list[dict[str, Any]], limit: int, offset: int) -> dict[str, Any]:
    return {"items": items, "limit": limit, "offset": offset}


def _event_values(payload: EventCreate, settings: Settings) -> dict[str, Any]:
    values = payload.model_dump()
    values["occurred_at"] = format_utc(payload.occurred_at)
    values["link_start_at"] = format_utc(
        payload.occurred_at - timedelta(seconds=settings.event_pre_roll_seconds)
    )
    values["link_end_at"] = format_utc(
        payload.occurred_at + timedelta(seconds=settings.event_post_roll_seconds)
    )
    if payload.snapshot_path is not None:
        relative, _target = normalize_relative_path(
            settings.snapshot_root, payload.snapshot_path
        )
        values["snapshot_path"] = relative
    return values


def build_internal_router() -> APIRouter:
    router = APIRouter(
        prefix="/internal/v1", dependencies=[Depends(require_internal_token)]
    )

    @router.post("/users", status_code=status.HTTP_201_CREATED)
    def create_user(payload: UserCreate, repository: Repo) -> dict[str, Any]:
        return repository.create_user(payload.model_dump(mode="json"))

    @router.get("/users/by-username/{username}")
    def get_user_by_username(username: str, repository: Repo) -> dict[str, Any]:
        user = repository.get_user_by_username(username)
        if user is None:
            raise _not_found("user")
        return user

    @router.get("/users")
    def list_users(
        repository: Repo,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        return _page(repository.list_users(limit, offset), limit, offset)

    @router.get("/users/{user_id}")
    def get_user(user_id: int, repository: Repo) -> dict[str, Any]:
        user = repository.get_user(user_id)
        if user is None:
            raise _not_found("user")
        return user

    @router.patch("/users/{user_id}")
    def update_user(
        user_id: int, payload: UserUpdate, repository: Repo
    ) -> dict[str, Any]:
        values = payload.model_dump(mode="json", exclude_unset=True)
        if any(value is None for value in values.values()):
            raise ApiError(422, "VALIDATION_ERROR", "사용자 필드는 null일 수 없습니다.")
        user = repository.update_user(user_id, values)
        if user is None:
            raise _not_found("user")
        return user

    @router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_user(user_id: int, repository: Repo) -> Response:
        if not repository.delete_user(user_id):
            raise _not_found("user")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.put("/users/{user_id}/camera-permissions/{camera_id}")
    def grant_camera_permission(
        user_id: int, camera_id: str, repository: Repo
    ) -> dict[str, Any]:
        validate_camera_id(camera_id)
        if repository.get_user(user_id) is None:
            raise _not_found("user")
        result = repository.grant_camera(user_id, camera_id)
        if result is None:
            raise _not_found("camera")
        return result

    @router.delete(
        "/users/{user_id}/camera-permissions/{camera_id}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    def revoke_camera_permission(
        user_id: int, camera_id: str, repository: Repo
    ) -> Response:
        if not repository.revoke_camera(user_id, camera_id):
            raise _not_found("camera_permission")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.get("/users/{user_id}/camera-permissions")
    def list_camera_permissions(user_id: int, repository: Repo) -> dict[str, Any]:
        if repository.get_user(user_id) is None:
            raise _not_found("user")
        return {"items": repository.list_user_cameras(user_id)}

    @router.post("/cameras", status_code=status.HTTP_201_CREATED)
    def create_camera(payload: CameraCreate, repository: Repo) -> dict[str, Any]:
        if repository.camera_count() >= 4:
            raise ApiError(
                409,
                "CAMERA_LIMIT_REACHED",
                "스키마 버전 1은 카메라를 최대 4대까지 지원합니다.",
            )
        return repository.create_camera(payload.model_dump(mode="json"))

    # Static path is intentionally registered before /cameras/{camera_id}.
    @router.get("/cameras/enabled")
    def enabled_cameras(
        repository: Repo,
        user_id: Annotated[int | None, Query(ge=1)] = None,
    ) -> dict[str, Any]:
        if user_id is not None and repository.get_user(user_id) is None:
            raise _not_found("user")
        items = repository.list_cameras(200, 0, enabled_only=True, user_id=user_id)
        return {"items": items}

    @router.get("/cameras")
    def list_cameras(
        repository: Repo,
        user_id: Annotated[int | None, Query(ge=1)] = None,
        enabled_only: bool = False,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        if user_id is not None and repository.get_user(user_id) is None:
            raise _not_found("user")
        return _page(
            repository.list_cameras(
                limit, offset, enabled_only=enabled_only, user_id=user_id
            ),
            limit,
            offset,
        )

    @router.get("/cameras/{camera_id}")
    def get_camera(camera_id: str, repository: Repo) -> dict[str, Any]:
        camera = repository.get_camera(camera_id)
        if camera is None:
            raise _not_found("camera")
        return camera

    @router.patch("/cameras/{camera_id}")
    def update_camera(
        camera_id: str, payload: CameraUpdate, repository: Repo
    ) -> dict[str, Any]:
        values = payload.model_dump(mode="json", exclude_unset=True)
        camera = repository.update_camera(camera_id, values)
        if camera is None:
            raise _not_found("camera")
        return camera

    @router.patch("/cameras/{camera_id}/status")
    def update_camera_status(
        camera_id: str, payload: CameraStatusUpdate, repository: Repo
    ) -> dict[str, Any]:
        camera = repository.update_camera(camera_id, {"status": payload.status.value})
        if camera is None:
            raise _not_found("camera")
        return camera

    @router.delete("/cameras/{camera_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_camera(camera_id: str, repository: Repo) -> Response:
        if not repository.delete_camera(camera_id):
            raise _not_found("camera")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.put("/cameras/{camera_id}/publish-credential")
    def put_camera_publish_credential(
        camera_id: str,
        payload: CameraPublishCredentialPut,
        repository: Repo,
    ) -> dict[str, Any]:
        validate_camera_id(camera_id)
        credential = repository.put_camera_publish_credential(
            camera_id, payload.username, payload.password_hash
        )
        if credential is None:
            raise _not_found("camera")
        return credential

    @router.get("/cameras/{camera_id}/publish-credential")
    def get_camera_publish_credential(
        camera_id: str, repository: Repo
    ) -> dict[str, Any]:
        validate_camera_id(camera_id)
        credential = repository.get_camera_publish_credential(camera_id)
        if credential is None:
            raise _not_found("camera_publish_credential")
        return credential

    @router.post("/recording-segments", status_code=status.HTTP_201_CREATED)
    def create_recording_segment(
        payload: RecordingSegmentCreate,
        repository: Repo,
        settings: RuntimeSettings,
    ) -> dict[str, Any]:
        segment, created = repository.create_segment(prepare_segment(payload, settings))
        repository.link_segment_to_events(
            segment,
            settings.event_pre_roll_seconds,
            settings.event_post_roll_seconds,
        )
        segment["idempotent_replay"] = not created
        return segment

    @router.get("/recording-segments/search")
    def search_recording_segments(
        repository: Repo,
        camera_id: str,
        from_time: Annotated[datetime, Query(alias="from")],
        to_time: Annotated[datetime, Query(alias="to")],
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        start = parse_utc(from_time)
        end = parse_utc(to_time)
        if end <= start:
            raise ApiError(422, "INVALID_TIME_RANGE", "to는 from보다 뒤여야 합니다.")
        items = repository.search_segments(
            camera_id, format_utc(start), format_utc(end), limit, offset
        )
        return _page(items, limit, offset)

    @router.get("/recording-segments/{segment_id}")
    def get_recording_segment(segment_id: int, repository: Repo) -> dict[str, Any]:
        segment = repository.get_segment(segment_id)
        if segment is None:
            raise _not_found("recording")
        return segment

    @router.post("/hooks/recording-complete", status_code=status.HTTP_201_CREATED)
    def recording_complete_hook(
        repository: Repo,
        settings: RuntimeSettings,
        camera_id: Annotated[str, Form()],
        segment_path: Annotated[str, Form()],
        duration_seconds: Annotated[float, Form(gt=0)],
    ) -> dict[str, Any]:
        validate_camera_id(camera_id)
        segment, created = repository.create_segment(
            prepare_recording_hook(
                camera_id=camera_id,
                segment_path=segment_path,
                duration_seconds=duration_seconds,
                settings=settings,
            )
        )
        repository.link_segment_to_events(
            segment,
            settings.event_pre_roll_seconds,
            settings.event_post_roll_seconds,
        )
        segment["idempotent_replay"] = not created
        return segment

    @router.post("/events", status_code=status.HTTP_201_CREATED)
    def create_event(
        payload: EventCreate, repository: Repo, settings: RuntimeSettings
    ) -> dict[str, Any]:
        if repository.get_camera(payload.camera_id) is None:
            raise _not_found("camera")
        return repository.create_event(_event_values(payload, settings))

    @router.get("/events")
    def search_events(
        repository: Repo,
        camera_id: str | None = None,
        event_type: str | None = None,
        from_time: Annotated[datetime | None, Query(alias="from")] = None,
        to_time: Annotated[datetime | None, Query(alias="to")] = None,
        limit: Annotated[int, Query(ge=1, le=200)] = 50,
        offset: Annotated[int, Query(ge=0)] = 0,
    ) -> dict[str, Any]:
        start = parse_utc(from_time) if from_time is not None else None
        end = parse_utc(to_time) if to_time is not None else None
        if start is not None and end is not None and end <= start:
            raise ApiError(422, "INVALID_TIME_RANGE", "to는 from보다 뒤여야 합니다.")
        items = repository.search_events(
            camera_id=camera_id,
            event_type=event_type,
            start_time=format_utc(start) if start else None,
            end_time=format_utc(end) if end else None,
            limit=limit,
            offset=offset,
        )
        return _page(items, limit, offset)

    @router.get("/events/{event_id}")
    def get_event(event_id: int, repository: Repo) -> dict[str, Any]:
        event = repository.get_event(event_id)
        if event is None:
            raise _not_found("event")
        return event

    @router.post("/tokens/refresh", status_code=status.HTTP_201_CREATED)
    def issue_refresh_token(
        payload: RefreshTokenCreate, repository: Repo
    ) -> dict[str, Any]:
        values = payload.model_dump(mode="json")
        values["expires_at"] = format_utc(payload.expires_at)
        try:
            return repository.issue_refresh_token(values)
        except LookupError as exc:
            raise _not_found("refresh_token") from exc
        except PermissionError as exc:
            raise ApiError(409, "REFRESH_TOKEN_NOT_ROTATABLE", str(exc)) from exc

    @router.get("/tokens/refresh/{jti}")
    def get_refresh_token(jti: str, repository: Repo) -> dict[str, Any]:
        token = repository.get_refresh_token(jti)
        if token is None:
            raise _not_found("refresh_token")
        return token

    @router.delete("/tokens/refresh/{jti}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_refresh_token(jti: str, repository: Repo) -> Response:
        if not repository.delete_refresh_token(jti):
            raise _not_found("refresh_token")
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @router.put("/tokens/revoked/{jti}")
    def put_revoked_token(
        jti: str, payload: RevokedTokenPut, repository: Repo
    ) -> dict[str, Any]:
        values = payload.model_dump(mode="json")
        values["expires_at"] = format_utc(payload.expires_at)
        return repository.put_revoked_token(jti, values)

    @router.get("/tokens/revoked/{jti}")
    def get_revoked_token(jti: str, repository: Repo) -> dict[str, Any]:
        token = repository.get_revoked_token(jti)
        if token is None:
            raise _not_found("revoked_token")
        return token

    @router.post("/reconcile")
    def reconcile_storage(
        repository: Repo, settings: RuntimeSettings
    ) -> dict[str, Any]:
        return reconcile(repository, settings)

    @router.post("/retention/cleanup")
    def clean_retention(
        payload: RetentionRequest,
        repository: Repo,
        settings: RuntimeSettings,
    ) -> dict[str, Any]:
        return retention_cleanup(repository, settings, payload)

    @router.post("/backup", status_code=status.HTTP_201_CREATED)
    def backup_database(
        payload: BackupRequest,
        repository: Repo,
        settings: RuntimeSettings,
    ) -> dict[str, Any]:
        filename = payload.filename or (
            "ai_cctv_" + format_utc(utc_now()).replace(":", "").replace("-", "") + ".db"
        )
        _relative, destination = normalize_relative_path(settings.backup_root, filename)
        if destination.exists():
            raise ApiError(409, "BACKUP_EXISTS", "동일한 이름의 백업이 이미 있습니다.")
        repository.database.backup(destination)
        relative = destination.relative_to(settings.backup_root.resolve()).as_posix()
        return {"relative_path": relative, "file_size": destination.stat().st_size}

    return router


def create_app(
    settings: Settings | None = None,
    repository: DataRepository | None = None,
) -> FastAPI:
    runtime_settings = settings or Settings.from_env()
    data_repository = repository or DataRepository(
        Database(
            runtime_settings.database_path,
            busy_timeout_ms=runtime_settings.busy_timeout_ms,
        )
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        runtime_settings.prepare_directories()
        data_repository.initialize()
        if data_repository.user_count() == 0:
            username = runtime_settings.initial_admin_username
            password_hash = runtime_settings.initial_admin_password_hash
            if username and password_hash:
                if not (3 <= len(username) <= 128) or any(
                    character in username for character in ("/", "\\", "\x00")
                ):
                    raise RuntimeError("INITIAL_ADMIN_USERNAME is invalid")
                if not password_hash.startswith("$argon2"):
                    raise RuntimeError(
                        "INITIAL_ADMIN_PASSWORD_HASH must be an Argon2 encoded hash"
                    )
                data_repository.create_user(
                    {
                        "username": username,
                        "password_hash": password_hash,
                        "role": "admin",
                        "is_active": True,
                    }
                )
        if (
            data_repository.camera_count() == 0
            and runtime_settings.config_path is not None
            and runtime_settings.config_path.is_file()
        ):
            bootstrap = load_config(runtime_settings.config_path)
            for camera in bootstrap.cameras:
                data_repository.create_camera(
                    {
                        "camera_id": camera.camera_id,
                        "name": camera.name,
                        "stream_path": camera.stream_path,
                        "edge_device_id": camera.edge_device_id,
                        "source_url": camera.source_url,
                        "enabled": camera.enabled,
                        "status": "offline" if camera.enabled else "disabled",
                    }
                )

        async def maintain_storage() -> None:
            while True:
                await asyncio.sleep(runtime_settings.maintenance_interval_seconds)
                try:
                    await asyncio.to_thread(
                        reconcile, data_repository, runtime_settings
                    )
                    await asyncio.to_thread(
                        retention_cleanup,
                        data_repository,
                        runtime_settings,
                        RetentionRequest(
                            retention_days=runtime_settings.retention_days,
                            dry_run=False,
                        ),
                    )
                except Exception:
                    LOGGER.exception("scheduled storage maintenance failed")

        maintenance_task = asyncio.create_task(
            maintain_storage(), name="data-storage-maintenance"
        )
        try:
            yield
        finally:
            maintenance_task.cancel()
            try:
                await maintenance_task
            except asyncio.CancelledError:
                pass

    application = FastAPI(
        title="AI_CCTV Data Service",
        version="0.3.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    application.state.settings = runtime_settings
    application.state.repository = data_repository
    install_error_handlers(application)

    @application.get("/health/live")
    def health_live() -> dict[str, str]:
        return {"status": "alive"}

    @application.get("/health/ready")
    def health_ready() -> dict[str, Any]:
        try:
            database = data_repository.database.health()
        except Exception as exc:
            raise ApiError(
                503, "DATABASE_NOT_READY", "SQLite를 사용할 수 없습니다."
            ) from exc
        if not storage_is_ready(runtime_settings):
            raise ApiError(
                503, "STORAGE_NOT_READY", "영속 저장소를 사용할 수 없습니다."
            )
        return {
            "status": "ready",
            "database": database,
            "storage": storage_usage(runtime_settings),
        }

    application.include_router(build_internal_router())
    return application


app = create_app()
