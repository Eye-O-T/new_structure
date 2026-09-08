# Data 서버의 시작·종료를 관리하고 내부 API와 녹화 정리·Edge 복구 작업을 실행한다.
"""FastAPI lifecycle and router composition for the Data service."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .api import build_internal_router
from .api.health import router as health_router
from .bootstrap import initialize_runtime
from .config import Settings
from .database.connection import Database
from .database.repositories import DataRepository
from .errors import install_error_handlers
from .storage.recordings import reconcile
from .workers.maintenance import maintain_storage
from .workers.recovery import recover_outages


def create_app(
    settings: Settings | None = None, repository: DataRepository | None = None
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
        initialize_runtime(data_repository, runtime_settings)
        await asyncio.to_thread(reconcile, data_repository, runtime_settings)
        tasks = [
            asyncio.create_task(
                maintain_storage(data_repository, runtime_settings),
                name="data-storage-maintenance",
            ),
            asyncio.create_task(
                recover_outages(data_repository, runtime_settings),
                name="data-edge-recovery",
            ),
        ]
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
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
    application.include_router(health_router)
    application.include_router(build_internal_router())
    return application


app = create_app()
