"""모델 준비 실패가 상태 서버를 막지 않고 복구 후 작업자가 다시 준비되는지 확인한다."""

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from server.services.analysis.app import main


@pytest.mark.asyncio
async def test_failed_initialization_reports_unavailable_then_recovers(monkeypatch):
    monkeypatch.setenv("DATA_ANALYSIS_TOKEN", "a" * 40)
    monkeypatch.setattr(main, "STARTUP_RETRY_SECONDS", 0.02)
    recovered = asyncio.Event()
    attempts = []
    closed = []

    @asynccontextmanager
    async def runtime(*args):
        attempts.append(args[-1])
        if len(attempts) == 1:
            raise RuntimeError("private model details must not appear in HTTP")
        recovered.set()
        try:
            yield SimpleNamespace(
                ready=True,
                stalled=False,
                last_error=None,
                last_outcome="complete",
                plugin=object(),
            )
        finally:
            closed.append(True)

    monkeypatch.setattr(main, "running_worker", runtime)
    app = main.create_app()
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://analysis"
        ) as client:
            assert (await client.get("/health/live")).status_code == 200
            failed = await client.get("/health/ready")
            assert failed.status_code == 503
            assert failed.json()["detail"]["last_error"] == "ANALYSIS_STARTUP_FAILED"
            assert "private" not in failed.text
            await asyncio.wait_for(recovered.wait(), timeout=2)
            ready = await client.get("/health/ready")
            assert ready.status_code == 200
            assert ready.json()["status"] == "ready"
            assert ready.json()["model_ready"] is True
    assert closed == [True]
    assert (
        attempts == ["server.services.analysis.processors:LocalAppearanceAnalyzer"] * 2
    )


@pytest.mark.asyncio
async def test_explicit_unconfigured_plugin_is_degraded():
    app = main.create_app()
    app.state.worker = SimpleNamespace(
        ready=True,
        stalled=False,
        last_error="PROCESSOR_UNCONFIGURED",
        last_outcome="unconfigured",
        plugin=object(),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://analysis"
    ) as client:
        response = await client.get("/health/ready")
    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["model_ready"] is False
