"""One container: camera detection threads and independent identity job consumer."""

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from ai_cctv_core.processing.runtime import running_worker, worker_status

from .data_client import DataClient
from .settings import Settings
from .supervisor import DetectionSupervisor

LOGGER = logging.getLogger("ai_cctv.preprocessing")


def create_app(settings: Settings | None = None):
    runtime_settings = settings or Settings.from_env()

    async def run_identity(app):
        # Plugin loading/execution may fail or be slow without blocking cameras.
        while True:
            try:
                async with running_worker(
                    "identity",
                    runtime_settings.identity_token,
                    runtime_settings.data_service_url,
                    runtime_settings.snapshots_root,
                    runtime_settings.identity_plugin,
                ) as worker:
                    app.state.identity_worker = worker
                    app.state.identity_error = None
                    await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise
            except Exception:
                app.state.identity_worker = None
                app.state.identity_error = "IDENTITY_STARTUP_FAILED"
                LOGGER.warning("identity initialization failed; detection continues")
            await asyncio.sleep(30)

    @asynccontextmanager
    async def lifespan(app):
        runtime_settings.validate()
        if len(runtime_settings.identity_token) < 32:
            raise ValueError("DATA_IDENTITY_TOKEN requires at least 32 characters")
        runtime_settings.snapshots_root.mkdir(parents=True, exist_ok=True)
        supervisor = DetectionSupervisor(
            runtime_settings,
            DataClient(
                runtime_settings.data_service_url,
                runtime_settings.internal_service_token,
            ),
        )
        app.state.supervisor = supervisor
        supervisor.start()
        identity_task = asyncio.create_task(run_identity(app), name="identity-runtime")
        try:
            yield
        finally:
            identity_task.cancel()
            try:
                await identity_task
            except asyncio.CancelledError:
                pass
            await asyncio.to_thread(supervisor.stop)

    app = FastAPI(
        title="AI_CCTV Preprocessing Service", version="0.3.0", lifespan=lifespan
    )
    app.state.supervisor = None
    app.state.identity_worker = None
    app.state.identity_error = None

    def status():
        supervisor = app.state.supervisor
        detection = (
            supervisor.status()
            if supervisor is not None
            else {"data_ready": False, "workers": {}}
        )
        identity = worker_status(app.state.identity_worker)
        if app.state.identity_error:
            identity["last_error"] = app.state.identity_error
        return {**detection, "identity": identity}

    @app.get("/health/live")
    def health_live():
        return {"status": "alive", "service": "preprocessing"}

    @app.get("/health/ready")
    def health_ready():
        state = status()
        if not state["data_ready"]:
            raise HTTPException(status_code=503, detail="Data Service is not ready")
        model_degraded = runtime_settings.inference_enabled and any(
            not worker["model_ready"] for worker in state["workers"].values()
        )
        identity = state["identity"]
        degraded = (
            model_degraded
            or not identity["ready"]
            or identity["stalled"]
            or identity["last_error"] is not None
        )
        return {"status": "degraded" if degraded else "ready", **state}

    @app.get("/internal/v1/status")
    def internal_status():
        return status()

    return app


app = create_app()
