"""Metadata analyzer service; a black box until an analysis plugin is configured."""

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException

from ai_cctv_core.processing.runtime import running_worker, worker_status


def create_app():
    @asynccontextmanager
    async def lifespan(app):
        async with running_worker(
            "analysis",
            os.getenv("DATA_ANALYSIS_TOKEN", ""),
            os.getenv("DATA_SERVICE_URL", "http://nginx:8080/internal/data/v1"),
            Path(os.getenv("SNAPSHOTS_ROOT", "/snapshots")),
            os.getenv(
                "ANALYSIS_PLUGIN",
                "server.services.analysis.processors:MetadataBlackBox",
            ),
        ) as worker:
            app.state.worker = worker
            yield

    app = FastAPI(title="AI_CCTV Analysis Service", version="0.3.0", lifespan=lifespan)
    app.state.worker = None

    @app.get("/health/live")
    def live():
        return {"status": "alive", "service": "analysis"}

    @app.get("/health/ready")
    def ready():
        status = worker_status(app.state.worker)
        if not status["ready"] or status["stalled"]:
            raise HTTPException(503, "Analysis worker is unavailable")
        return {
            "status": "degraded" if status["last_error"] else "ready",
            **status,
        }

    return app


app = create_app()
