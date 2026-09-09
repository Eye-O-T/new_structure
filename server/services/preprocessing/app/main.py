# preprocessing 컨테이너의 시작점이다. 카메라별 사람 탐지와 카메라 간 인물 식별을
# 같은 컨테이너에서 실행하되, 한쪽의 작업 지연이 다른 쪽을 막지 않도록 따로 구동한다.

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from ai_cctv_core.processing.runtime import running_worker, worker_status

from .data_client import DataClient
from .settings import Settings
from .supervisor import DetectionSupervisor

LOGGER = logging.getLogger("ai_cctv.preprocessing")


# 탐지 감독자와 비동기 식별 작업자를 조립하고 두 처리 경로의 상태를 노출한다.
def create_app(settings: Settings | None = None):
    runtime_settings = settings or Settings.from_env()

    async def run_identity(app):
        # 인물 식별 모델의 준비가 실패하면 일정 시간 뒤 다시 시도한다.
        # 그동안 카메라 탐지는 독립적인 스레드에서 계속 실행한다.
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
        # 시작 전에 설정을 검증하고 카메라 관리자와 식별 작업자를 함께 준비한다.
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
            # 종료 순서를 명시해 백그라운드 작업과 영상 연결이 남지 않게 정리한다.
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

    # 카메라별 탐지 상태와 식별 준비·오류 상태를 하나의 응답으로 합친다.
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

    # Data 연결 실패는 503으로, 연결 이후 모델·식별 장애는 degraded 상태로 구분한다.
    @app.get("/health/ready")
    def health_ready():
        state = status()
        if not state["data_ready"]:
            raise HTTPException(status_code=503, detail="Data Service is not ready")
        # Data에 연결되면 HTTP 200을 유지할 수 있지만, 모델·식별 장애는 degraded로
        # 표시한다. 따라서 HTTP 성공 여부만으로 모든 기능이 정상이라고 판단하면 안 된다.
        model_degraded = runtime_settings.inference_enabled and any(
            not worker["model_ready"]
            or worker.get("state") in {"offline", "stopped", "error"}
            or worker.get("event_delivery_error")
            or worker.get("frame_stale")
            for worker in state["workers"].values()
        )
        identity = state["identity"]
        delivery = state.get("event_delivery", {})
        degraded = (
            model_degraded
            or not identity["ready"]
            or not identity["model_ready"]
            or identity["stalled"]
            or identity["last_error"] is not None
            or bool(delivery.get("last_error"))
            or bool(delivery.get("rejected"))
        )
        return {"status": "degraded" if degraded else "ready", **state}

    @app.get("/internal/v1/status")
    def internal_status():
        return status()

    return app


app = create_app()
