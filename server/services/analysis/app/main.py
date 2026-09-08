"""Metadata analyzer service; a black box until an analysis plugin is configured."""

# analysis 컨테이너는 Data에서 받은 사람 관측을 분석하고 추가 정보(metadata)를 돌려준다.
# 영상 탐지나 전역 인물 ID 지정은 맡지 않으며, 모델은 플러그인으로 별도 연결한다.

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException

from ai_cctv_core.processing.runtime import running_worker, worker_status


def create_app():
    @asynccontextmanager
    async def lifespan(app):
        # 공통 작업자는 인증·입력 검증·결과 보고를 맡고 실제 모델 호출은 플러그인에 위임한다.
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
            # 작업을 가져올 수 없거나 모델 시간초과로 멈췄으면 처리 준비가 되지 않은 상태다.
            raise HTTPException(503, "Analysis worker is unavailable")
        # ready는 작업 처리 통로의 상태다. 기본 블랙박스의 unconfigured 결과는
        # 실제 분석 모델이 아직 연결되지 않았다는 뜻이며 분석 성능 검증 완료를 뜻하지 않는다.
        return {
            "status": "degraded" if status["last_error"] else "ready",
            **status,
        }

    return app


app = create_app()
