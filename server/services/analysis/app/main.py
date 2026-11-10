# analysis 컨테이너는 Data에서 받은 사람 관측을 분석하고 추가 정보(metadata)를 돌려준다.
# 영상 탐지나 전역 인물 ID 지정은 맡지 않으며, 모델은 플러그인으로 별도 연결한다.

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException

from ai_cctv_core.processing.isolation import ProcessorStartupError
from ai_cctv_core.processing.runtime import running_worker, worker_status

LOGGER = logging.getLogger("ai_cctv.analysis")
DEFAULT_ANALYSIS_PLUGIN = (
    "server.services.analysis.processors.attributes:PersonAttributeAnalyzer"
)
ANALYSIS_PLUGIN_ALIASES = {
    DEFAULT_ANALYSIS_PLUGIN,
    "server.services.analysis.processors:PersonAttributeAnalyzer",
    "server.services.analysis.processors:LocalAppearanceAnalyzer",
    "server.services.analysis.processors.appearance:LocalAppearanceAnalyzer",
}


# 분석 전용 토큰과 플러그인으로 작업자를 만들고 프로세스 생존·작업 준비를 따로 보고한다.
def create_app():
    @asynccontextmanager
    async def lifespan(app):
        if len(os.getenv("DATA_ANALYSIS_TOKEN", "")) < 32:
            raise ValueError("DATA_ANALYSIS_TOKEN requires at least 32 characters")
        plugin_reference = os.getenv("ANALYSIS_PLUGIN") or DEFAULT_ANALYSIS_PLUGIN
        if plugin_reference not in ANALYSIS_PLUGIN_ALIASES:
            app.state.startup_error = "ANALYSIS_PLUGIN_INVALID"
            LOGGER.error("analysis startup failed code=%s", app.state.startup_error)
            raise RuntimeError(app.state.startup_error)
        try:
            async with running_worker(
                "analysis",
                os.getenv("DATA_ANALYSIS_TOKEN", ""),
                os.getenv("DATA_SERVICE_URL", "http://nginx:8080/internal/data/v1"),
                Path(os.getenv("SNAPSHOTS_ROOT", "/snapshots")),
                plugin_reference,
            ) as worker:
                app.state.worker = worker
                yield
        except ProcessorStartupError as exc:
            app.state.startup_error = exc.code
            LOGGER.error("analysis startup failed code=%s", app.state.startup_error)
            raise RuntimeError(app.state.startup_error) from None
        except TimeoutError:
            app.state.startup_error = "ANALYSIS_MODEL_STARTUP_TIMEOUT"
            LOGGER.error("analysis startup failed code=%s", app.state.startup_error)
            raise RuntimeError(app.state.startup_error) from None
        except Exception:
            app.state.startup_error = "ANALYSIS_STARTUP_FAILED"
            LOGGER.error("analysis startup failed code=%s", app.state.startup_error)
            raise RuntimeError(app.state.startup_error) from None

    app = FastAPI(title="AI_CCTV Analysis Service", version="0.3.0", lifespan=lifespan)
    app.state.worker = None
    app.state.startup_error = None

    @app.get("/health/live")
    def live():
        return {"status": "alive", "service": "analysis"}

    @app.get("/health/ready")
    def ready():
        status = worker_status(app.state.worker)
        status["gpu_requested"] = os.getenv("PREPROCESSING_GPU", "false").strip().lower() in {"1", "true", "yes", "on"}
        if app.state.startup_error:
            status["last_error"] = app.state.startup_error
            raise HTTPException(503, {"status": "unavailable", **status})
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
