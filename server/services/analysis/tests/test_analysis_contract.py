import json

import httpx
import pytest
from fastapi.testclient import TestClient

from ai_cctv_core.processing.worker import ObjectWorker
from server.services.analysis.app.main import create_app
from server.services.analysis.processors import MetadataBlackBox


@pytest.mark.asyncio
@pytest.mark.parametrize("assign_identity", [False, True])
async def test_analysis_contract_cannot_assign_global_identity(
    tmp_path, assign_identity
):
    (tmp_path / "object.jpg").write_bytes(b"No image analysis occurs in this test")
    job = {
        "id": 7,
        "event_id": 11,
        "camera_id": "cam-001",
        "person_id": "3",
        "lease_id": "b" * 32,
        "object_observation": {
            "schema_version": 1,
            "tracking_session_id": "a" * 32,
            "frame_width": 100,
            "frame_height": 100,
            "bbox": [1, 2, 30, 40],
            "crop_path": "object.jpg",
        },
    }
    sent = []

    def transport(request):
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json={"job": job})
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"accepted": True})

    class InvalidAnalyzer:
        def process(self, job, crop):
            return {"outcome": "complete", "global_person_id": "forbidden"}

    plugin = InvalidAnalyzer() if assign_identity else MetadataBlackBox()
    async with httpx.AsyncClient(
        base_url="http://data", transport=httpx.MockTransport(transport)
    ) as client:
        worker = ObjectWorker("analysis", client, tmp_path, plugin)
        assert await worker.once()
    result = sent[0]
    assert result["lease_id"] == job["lease_id"]
    assert result["global_person_id"] is None
    assert result["outcome"] == ("failed" if assign_identity else "unconfigured")
    if not assign_identity:
        assert result["metadata"] == {"reason": "metadata_backend_not_implemented"}
    assert worker.last_outcome == result["outcome"]


def test_analysis_health_requires_started_data_consumer():
    client = TestClient(create_app())
    assert client.get("/health/live").json() == {
        "status": "alive",
        "service": "analysis",
    }
    assert client.get("/health/ready").status_code == 503
