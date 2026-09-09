# Data 서버의 시작·종료를 관리하고 내부 API와 녹화 정리·Edge 복구 작업을 실행한다.

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
from .workers.supervision import WorkerStatus, supervise


# 설정·저장소를 주입할 수 있는 앱 팩터리로 운영 실행과 테스트의 구성 경계를 제공한다.
def create_app(
    settings: Settings | None = None, repository: DataRepository | None = None
) -> FastAPI:
    runtime_settings = settings or Settings.from_env()
    data_repository = repository or DataRepository(
        Database(
            runtime_settings.database_path,
            busy_timeout_ms=runtime_settings.busy_timeout_ms,
        ),
        identity_match_threshold=runtime_settings.identity_match_threshold,
        identity_match_margin=runtime_settings.identity_match_margin,
    )

    # 요청 수신 전에 초기화와 파일 대조를 마치고 종료 시 백그라운드 작업 취소를 기다린다.
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        initialize_runtime(data_repository, runtime_settings)
        await asyncio.to_thread(reconcile, data_repository, runtime_settings)
        worker_states = {name: WorkerStatus() for name in ("storage", "recovery")}
        _app.state.workers = worker_states
        tasks = [
            asyncio.create_task(
                supervise(
                    lambda: maintain_storage(
                        data_repository, runtime_settings, worker_states["storage"]
                    ),
                    worker_states["storage"],
                ),
                name="data-storage-maintenance",
            ),
            asyncio.create_task(
                supervise(
                    lambda: recover_outages(
                        data_repository, runtime_settings, worker_states["recovery"]
                    ),
                    worker_states["recovery"],
                ),
                name="data-edge-recovery",
            ),
        ]
        _app.state.worker_tasks = dict(zip(("storage", "recovery"), tasks))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            try:
                await asyncio.gather(*tasks, return_exceptions=True)
            except asyncio.CancelledError:
                pass
            scanner = getattr(data_repository, "snapshot_scanner", None)
            if scanner is not None:
                scanner.close()

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
