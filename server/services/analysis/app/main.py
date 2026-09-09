# analysis 컨테이너는 Data에서 받은 사람 관측을 분석하고 추가 정보(metadata)를 돌려준다.
# 영상 탐지나 전역 인물 ID 지정은 맡지 않으며, 모델은 플러그인으로 별도 연결한다.

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException

from ai_cctv_core.processing.runtime import running_worker, worker_status

LOGGER = logging.getLogger("ai_cctv.analysis")
STARTUP_RETRY_SECONDS = 30


# 분석 전용 토큰과 플러그인으로 작업자를 만들고 프로세스 생존·작업 준비를 따로 보고한다.
def create_app():
    async def run_analysis(app):
        # 모델 파일 교체나 일시적인 준비 실패 후에도 컨테이너를 재생성할 필요가 없다.
        while True:
            try:
                async with running_worker(
                    "analysis",
                    os.getenv("DATA_ANALYSIS_TOKEN", ""),
                    os.getenv("DATA_SERVICE_URL", "http://nginx:8080/internal/data/v1"),
                    Path(os.getenv("SNAPSHOTS_ROOT", "/snapshots")),
                    os.getenv(
                        "ANALYSIS_PLUGIN",
                        "server.services.analysis.processors:LocalAppearanceAnalyzer",
                    ),
                ) as worker:
                    app.state.worker = worker
                    app.state.startup_error = None
                    await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise
            except Exception:
                app.state.worker = None
                app.state.startup_error = "ANALYSIS_STARTUP_FAILED"
                LOGGER.warning("analysis initialization failed; retrying in 30 seconds")
            await asyncio.sleep(STARTUP_RETRY_SECONDS)

    @asynccontextmanager
    async def lifespan(app):
        if len(os.getenv("DATA_ANALYSIS_TOKEN", "")) < 32:
            raise ValueError("DATA_ANALYSIS_TOKEN requires at least 32 characters")
        # 준비 중에도 liveness를 응답하고 readiness로 처리 가능 여부를 구분한다.
        task = asyncio.create_task(run_analysis(app), name="analysis-runtime")
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app = FastAPI(title="AI_CCTV Analysis Service", version="0.3.0", lifespan=lifespan)
    app.state.worker = None
    app.state.startup_error = None

    @app.get("/health/live")
    def live():
        return {"status": "alive", "service": "analysis"}

    @app.get("/health/ready")
    def ready():
        status = worker_status(app.state.worker)
        if app.state.startup_error:
            status["last_error"] = app.state.startup_error
        if not status["ready"] or status["stalled"]:
            # 작업을 가져올 수 없거나 모델 시간초과로 멈췄으면 처리 준비가 되지 않은 상태다.
            raise HTTPException(503, {"status": "unavailable", **status})
        return {
            "status": "degraded"
            if status["last_error"] or not status["model_ready"]
            else "ready",
            **status,
        }

    return app


app = create_app()
