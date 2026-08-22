from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from .data_client import DataClient
from .settings import Settings
from .supervisor import InferenceSupervisor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s service=inference %(message)s",
)

settings = Settings.from_env()
supervisor: InferenceSupervisor | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global supervisor
    settings.validate()
    settings.snapshots_root.mkdir(parents=True, exist_ok=True)
    supervisor = InferenceSupervisor(
        settings,
        DataClient(settings.data_service_url, settings.internal_service_token),
    )
    supervisor.start()
    yield
    supervisor.stop()


app = FastAPI(title="AI_CCTV Inference Service", version="0.3.0", lifespan=lifespan)


@app.get("/health/live")
def health_live():
    return {"status": "alive", "service": "inference"}


@app.get("/health/ready")
def health_ready():
    if supervisor is None or not supervisor.data_ready:
        raise HTTPException(status_code=503, detail="Data Service is not ready")
    status = supervisor.status()
    model_degraded = settings.inference_enabled and any(
        not worker["model_ready"] for worker in status["workers"].values()
    )
    return {"status": "degraded" if model_degraded else "ready", **status}


@app.get("/internal/v1/status")
def internal_status():
    return supervisor.status() if supervisor else {"data_ready": False, "workers": {}}
