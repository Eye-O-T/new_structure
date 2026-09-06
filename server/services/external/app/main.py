"""External service lifecycle and public/internal router composition."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api import (
    auth,
    cameras,
    events,
    health,
    media_auth,
    notifications,
    objects,
    recordings,
    users,
)
from .camera_lifecycle import CameraLifecycleLocks
from .clients.data import DataClient
from .config import Settings
from .dependencies import get_settings_dependency
from .errors import register_exception_handlers
from .version import SERVICE_VERSION
from .workers.push_dispatcher import PushDispatcher
from .workers.status_collector import StatusCollector


@asynccontextmanager
async def _lifespan(application: FastAPI):
    collector_task: asyncio.Task[None] | None = None
    push_task: asyncio.Task[None] | None = None
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
        if settings.push_enabled:
            dispatcher = PushDispatcher(settings, client)
            push_task = asyncio.create_task(dispatcher.run(), name="external-fcm-push")
    try:
        yield
    finally:
        if push_task is not None:
            push_task.cancel()
            await asyncio.gather(push_task, return_exceptions=True)
        if collector_task is not None:
            collector_task.cancel()
            await asyncio.gather(collector_task, return_exceptions=True)
        if client is not None and (owns_client or hasattr(client, "close")):
            await client.close()


def create_app(
    settings: Settings | None = None, data_client: DataClient | None = None
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
        application.dependency_overrides[get_settings_dependency] = lambda: settings
    if data_client is not None:
        application.state.data_client = data_client
    application.state.camera_lifecycle_lock_factory = CameraLifecycleLocks()
    register_exception_handlers(application)
    for router in (
        notifications.router,
        health.router,
        auth.router,
        cameras.router,
        objects.router,
        recordings.router,
        events.router,
        users.router,
        media_auth.router,
    ):
        application.include_router(router)
    return application


app = create_app()
