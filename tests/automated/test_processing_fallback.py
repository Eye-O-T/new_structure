"""실제 JPEG·기본 CPU 플러그인·객체 작업자·Data HTTP를 연결한 통합 검증이다."""

from contextlib import AsyncExitStack, asynccontextmanager
import json
import math
import uuid

import cv2
import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from ai_cctv_core.processing.worker import ObjectWorker
from ai_cctv_core.time import format_utc, utc_now
from server.services.analysis.processors import LocalAppearanceAnalyzer
from server.services.data.app.config import Settings
from server.services.data.app.main import create_app
from server.services.preprocessing.processors.identity import LocalAppearanceIdentity
from tests.automated import test_object_processing as object_tests


BASE = "/internal/v1"
TOKENS = object_tests.TOKENS
objects = object_tests.objects
BBOX = [48, 32, 176, 288]
RED_BLUE = ((0, 0, 255), (255, 0, 0))
GREEN_YELLOW = ((0, 255, 0), (0, 255, 255))


def _headers(scope):
    return {"X-Internal-Token": TOKENS[scope]}


def _appearance(
    client, settings, *, camera="cam-001", person="7", session="a" * 32, colors=RED_BLUE
):
    # 상·하체 두 영역에 실제 픽셀을 채우고, bbox와 정확히 같은 128×256 JPEG를 저장한다.
    image = np.empty((256, 128, 3), dtype=np.uint8)
    image[:140] = colors[0]
    image[140:] = colors[1]
    source_id = uuid.uuid4().hex
    relative = f"{camera}/{source_id}.jpg"
    crop = settings.snapshot_root / relative
    crop.parent.mkdir(parents=True, exist_ok=True)
    assert cv2.imwrite(str(crop), image, [cv2.IMWRITE_JPEG_QUALITY, 95])
    response = client.post(
        f"{BASE}/events",
        headers=_headers("inference"),
        json={
            "source_event_id": source_id,
            "camera_id": camera,
            "person_id": person,
            "event_type": "person_appeared",
            "occurred_at": format_utc(utc_now()),
            "object_observation": {
                "tracking_session_id": session,
                "bbox": BBOX,
                "frame_width": 320,
                "frame_height": 320,
                "crop_path": relative,
            },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _event(client, event_id):
    response = client.get(f"{BASE}/events/{event_id}", headers=_headers("external"))
    assert response.status_code == 200, response.text
    return response.json()


@asynccontextmanager
async def _workers(client, settings, sent=None):
    # ASGI가 실제 Data 인증·검증·저장 경로를 실행한다. 플러그인 출력이나 완료 응답은 대체하지 않는다.
    async def capture(request):
        if sent is not None and request.url.path.endswith("/complete"):
            sent.append((request.url.path, json.loads(request.content)))

    async with AsyncExitStack() as stack:
        workers = {}
        for stage, plugin in (
            ("identity", LocalAppearanceIdentity()),
            ("analysis", LocalAppearanceAnalyzer()),
        ):
            transport = await stack.enter_async_context(
                httpx.AsyncClient(
                    base_url=f"http://data{BASE}/",
                    transport=httpx.ASGITransport(app=client.app),
                    headers=_headers(stage),
                    event_hooks={"request": [capture]},
                )
            )
            workers[stage] = ObjectWorker(
                stage, transport, settings.snapshot_root, plugin
            )
        yield workers


async def _complete(workers, order=("identity", "analysis")):
    for stage in order:
        worker = workers[stage]
        assert await worker.once()
        assert worker.last_outcome == "complete", worker.last_error
        assert worker.last_error is None
        assert worker.ready and not worker.stalled


def _assert_colors(event, upper, lower):
    result = event["metadata"]["analysis"]["result"]
    assert result["backend"] == "local_appearance"
    assert result["image"]["width"] == 128
    assert result["image"]["height"] == 256
    colors = result["clothing_colors"]
    assert colors["upper"]["dominant_color"]["name"] == upper
    assert colors["lower"]["dominant_color"]["name"] == lower
    assert result["evidence"]["confidence_is_probability"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("order", [("identity", "analysis"), ("analysis", "identity")])
async def test_default_processors_merge_pixel_results_in_either_completion_order(
    objects, order
):
    client, repo, settings = objects
    event = _appearance(client, settings)
    sent = []
    async with _workers(client, settings, sent) as workers:
        await _complete(workers, order[:1])
        partial = _event(client, event["id"])
        assert partial["metadata"][order[0]]["status"] == "complete"
        assert partial["metadata"][order[1]]["status"] == "pending"
        await _complete(workers, order[1:])
        for worker in workers.values():
            assert not await worker.once()

    saved = _event(client, event["id"])
    assert saved["global_person_id"].startswith("person-")
    assert saved["metadata"][order[0]] == partial["metadata"][order[0]]
    assert saved["metadata"]["identity"]["status"] == "complete"
    assert saved["metadata"]["analysis"]["status"] == "complete"
    identity = saved["metadata"]["identity"]["result"]
    assert identity["backend"] == "appearance-hsv-v1"
    assert identity["match"]["decision"] == "new"
    _assert_colors(saved, "red", "blue")

    # 특징은 내부 완료 계약을 통과하지만 일반 이벤트 반환·분석 결과에는 복사되지 않는다.
    identity_payload = next(payload for path, payload in sent if "/identity/" in path)
    features = identity_payload["identity_descriptor"]["features"]
    assert 16 <= len(features) <= 2048
    assert all(math.isfinite(value) for value in features)
    assert math.hypot(*features) == pytest.approx(1.0, abs=0.001)
    assert identity_payload["identity_descriptor"]["space_id"] == "appearance-hsv-v1"
    assert "features" not in json.dumps(saved)
    assert "identity_descriptor" not in json.dumps(saved)
    analysis_payload = next(payload for path, payload in sent if "/analysis/" in path)
    assert "identity_descriptor" not in analysis_payload
    with repo.database.connection() as connection:
        assert {
            row[0] for row in connection.execute("SELECT state FROM object_jobs")
        } == {"complete"}
        assert (
            connection.execute("SELECT COUNT(*) FROM identity_gallery").fetchone()[0]
            == 1
        )


@pytest.mark.asyncio
async def test_same_pixels_match_across_cameras_and_different_colors_create_new_identity(
    objects,
):
    client, repo, settings = objects
    repo.create_camera(
        {"camera_id": "cam-003", "name": "Third", "stream_path": "cam-003"}
    )
    async with _workers(client, settings) as workers:
        first = _appearance(client, settings)
        await _complete(workers)
        second = _appearance(client, settings, camera="cam-002", session="b" * 32)
        await _complete(workers, ("analysis", "identity"))
        # 세 번째 카메라를 써서 새 ID가 같은 카메라 충돌 차단 때문인 경우를 배제한다.
        third = _appearance(client, settings, camera="cam-003", colors=GREEN_YELLOW)
        await _complete(workers)
    first, second, third = (
        _event(client, value["id"]) for value in (first, second, third)
    )
    assert first["global_person_id"] == second["global_person_id"]
    assert third["global_person_id"] != first["global_person_id"]
    second_match = second["metadata"]["identity"]["result"]["match"]
    third_match = third["metadata"]["identity"]["result"]["match"]
    assert second_match["decision"] == "matched"
    assert second_match["similarity"] == pytest.approx(1.0)
    assert third_match["decision"] == "new"
    assert third_match["similarity"] < 0.97
    _assert_colors(second, "red", "blue")
    _assert_colors(third, "green", "yellow")


@pytest.mark.asyncio
async def test_data_and_processor_restart_preserves_track_identity_and_live_box_link(
    tmp_path,
):
    settings = Settings(
        database_path=tmp_path / "db.sqlite",
        storage_root=tmp_path / "recordings",
        snapshot_root=tmp_path / "snapshots",
        backup_root=tmp_path / "backups",
        internal_token="",
        **{f"data_{name}_token": token for name, token in TOKENS.items()},
    )
    with TestClient(create_app(settings)) as client:
        client.app.state.repository.create_camera(
            {"camera_id": "cam-001", "name": "First", "stream_path": "cam-001"}
        )
        first = _appearance(client, settings)
        async with _workers(client, settings) as workers:
            await _complete(workers)
        original = _event(client, first["id"])

    # 앱 수명과 플러그인 객체를 모두 종료한 뒤 같은 DB를 연다. 변경된 색도 기존 track 연결을 바꾸지 않는다.
    with TestClient(create_app(settings)) as restarted:
        assert _event(restarted, first["id"])["metadata"] == original["metadata"]
        second = _appearance(restarted, settings, colors=GREEN_YELLOW)
        assert second["global_person_id"] == original["global_person_id"]
        async with _workers(restarted, settings) as workers:
            await _complete(workers, ("analysis", "identity"))
        saved = _event(restarted, second["id"])
        assert saved["global_person_id"] == original["global_person_id"]
        assert (
            saved["metadata"]["identity"]["result"]["match"]["decision"]
            == "existing_track"
        )
        _assert_colors(saved, "green", "yellow")
        live = restarted.put(
            f"{BASE}/cameras/cam-001/objects",
            headers=_headers("inference"),
            json={
                "tracking_session_id": "a" * 32,
                "observed_at": format_utc(utc_now()),
                "frame_width": 320,
                "frame_height": 320,
                "objects": [{"person_id": "7", "bbox": BBOX, "confidence": 0.9}],
            },
        )
        assert live.status_code == 200, live.text
        response = restarted.get(
            f"{BASE}/cameras/cam-001/objects", headers=_headers("external")
        )
        assert response.status_code == 200, response.text
        current = response.json()
        assert current["stale"] is False
        assert current["objects"][0]["bbox"] == BBOX
        assert current["objects"][0]["global_person_id"] == original["global_person_id"]
