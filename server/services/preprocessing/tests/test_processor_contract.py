from contextlib import asynccontextmanager
import asyncio
from dataclasses import replace

import httpx
import numpy as np
import pytest
from pydantic import ValidationError

from ai_cctv_core.processing.plugins import load_factory
from ai_cctv_core.time import utc_now
from server.services.preprocessing.app.settings import Settings
from server.services.preprocessing.processors.detection.contracts import (
    DetectionFrame,
    DetectionResult,
)


def test_frame_and_result_contract_reject_incompatible_plugins():
    frame = DetectionFrame(
        camera_id="cam-001",
        tracking_session_id="a" * 32,
        observed_at=utc_now(),
        image=np.zeros((40, 60, 3), dtype=np.uint8),
    )
    assert frame.schema_version == 1
    assert "image" not in frame.model_dump()
    with pytest.raises(ValidationError):
        DetectionFrame.model_validate({**frame.model_dump(), "image": np.zeros((1, 1))})
    with pytest.raises(ValidationError):
        DetectionResult(schema_version=2)
    with pytest.raises(ValidationError):
        DetectionResult(
            objects=[{"person_id": "1", "bbox": [0, 0, 2, 2], "confidence": 3}]
        )
    # Importing the default adapter doesn't load ultralytics or a model.
    factory = load_factory(
        "server.services.preprocessing.processors.detection.yolo:YoloTracker"
    )
    assert callable(factory.process)


@pytest.mark.asyncio
@pytest.mark.parametrize("identity_failure", [False, True], ids=["slow", "failing"])
async def test_identity_startup_does_not_block_detection(
    tmp_path, monkeypatch, identity_failure
):
    from server.services.preprocessing.app import main

    settings = replace(
        Settings.from_env(),
        internal_service_token="p" * 40,
        identity_token="i" * 40,
        media_read_username="reader",
        media_read_password="m" * 40,
        snapshots_root=tmp_path,
        inference_enabled=False,
    )
    state = {"started": False, "stopped": False}

    class Supervisor:
        def __init__(self, settings, client):
            self.client = client

        def start(self):
            state["started"] = True

        def stop(self):
            state["stopped"] = True
            self.client.close()

        def status(self):
            return {"data_ready": True, "workers": {}}

    @asynccontextmanager
    async def identity(*args):
        if identity_failure:
            raise RuntimeError("test plugin failed")
        await asyncio.Event().wait()
        yield

    monkeypatch.setattr(main, "DetectionSupervisor", Supervisor)
    monkeypatch.setattr(main, "running_worker", identity)
    app = main.create_app(settings)
    async with app.router.lifespan_context(app):
        await asyncio.sleep(0)
        assert state["started"]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://preprocessing"
        ) as client:
            result = await client.get("/health/ready")
        assert result.status_code == 200
        assert result.json()["status"] == "degraded"
        assert result.json()["data_ready"] is True
        assert result.json()["identity"]["ready"] is False
        if identity_failure:
            assert result.json()["identity"]["last_error"] == "IDENTITY_STARTUP_FAILED"
    assert state["stopped"]
