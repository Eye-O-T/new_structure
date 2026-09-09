"""OSNET_TEST_MODEL_PATH 지정 시 실가중치·OpenCV·격리 작업자·Data HTTP를 검증한다.

모델을 내려받거나 인식 정확도를 평가하지 않는다. 앞뒤 모습 연결 성능은 별도 데이터로
측정해야 하며, 여기서는 실제 512차원 추론과 저장 계약만 검증한다.
"""

import hashlib
import json
import os
from pathlib import Path

import httpx
import numpy as np
import pytest

from ai_cctv_core.processing.isolation import IsolatedProcessor
from ai_cctv_core.processing.worker import ObjectWorker
from server.services.preprocessing.processors.identity import OsNetIdentity
from server.services.preprocessing.tests.test_local_identity import _crop, _person
from tests.automated import test_object_processing as object_tests
from tests.automated.test_processing_fallback import _appearance, _event


objects = object_tests.objects


@pytest.fixture
def real_model():
    configured = os.getenv("OSNET_TEST_MODEL_PATH")
    if not configured:
        pytest.skip("Set OSNET_TEST_MODEL_PATH to test an exported OSNet x0.25 model")
    path = Path(configured).resolve(strict=True)
    assert path.is_file()
    return path


def configured_osnet():
    # 테스트의 로컬 모델 폴더를 자식에 전달한다. 운영 컨테이너는 기본 /models를 사용한다.
    path = Path(os.environ["OSNET_TEST_MODEL_PATH"]).resolve(strict=True)
    return OsNetIdentity(path, models_root=path.parent)


def test_real_osnet_produces_repeatable_finite_embeddings_that_depend_on_pixels(
    real_model, tmp_path
):
    processor = OsNetIdentity(real_model, models_root=real_model.parent)
    job, crop = _crop(tmp_path, _person())
    first = processor.process(job, crop)
    repeated = processor.process(job, crop)
    np.testing.assert_array_equal(
        first["identity_descriptor"]["features"],
        repeated["identity_descriptor"]["features"],
    )
    # 단순 색 배치 차이로 출력이 달라지는지 확인할 뿐 동일인/타인 판정을 주장하지 않는다.
    job, crop = _crop(tmp_path, _person()[::-1])
    changed = processor.process(job, crop)
    for result in (first, changed):
        features = np.asarray(result["identity_descriptor"]["features"])
        assert features.shape == (512,)
        assert np.isfinite(features).all()
        assert np.linalg.norm(features) == pytest.approx(1.0, abs=1e-6)
        assert result["metadata"]["backend"] == "osnet"
        assert result["metadata"]["architecture"] == "osnet_x0_25"
    assert not np.allclose(
        first["identity_descriptor"]["features"],
        changed["identity_descriptor"]["features"],
        atol=1e-6,
    )
    assert (
        first["metadata"]["model_sha256"]
        == hashlib.sha256(real_model.read_bytes()).hexdigest()
    )


@pytest.mark.asyncio
async def test_real_osnet_in_spawned_worker_completes_data_jobs_and_links_same_image_across_cameras(
    real_model, objects
):
    client, repo, settings = objects
    sent = []

    async def capture(request):
        if request.url.path.endswith("/complete"):
            sent.append(json.loads(request.content))

    processor = IsolatedProcessor(
        "tests.automated.test_osnet_processing:configured_osnet",
        timeout_seconds=30,
        startup_timeout_seconds=30,
    )
    try:
        async with httpx.AsyncClient(
            base_url="http://data/internal/v1/",
            transport=httpx.ASGITransport(app=client.app),
            headers={"X-Internal-Token": object_tests.TOKENS["identity"]},
            event_hooks={"request": [capture]},
        ) as transport:
            worker = ObjectWorker(
                "identity",
                transport,
                settings.snapshot_root,
                processor,
                timeout_seconds=35,
            )
            first = _appearance(client, settings)
            assert await worker.once()
            assert worker.last_outcome == "complete", worker.last_error
            second = _appearance(client, settings, camera="cam-002", session="b" * 32)
            assert await worker.once()
            assert worker.last_outcome == "complete", worker.last_error
            assert not await worker.once()
    finally:
        processor.close()
    saved_first = _event(client, first["id"])
    saved_second = _event(client, second["id"])
    assert saved_first["global_person_id"] == saved_second["global_person_id"]
    assert saved_first["global_person_id"].startswith("person-")
    assert (
        saved_second["metadata"]["identity"]["result"]["match"]["decision"] == "matched"
    )
    assert saved_second["metadata"]["identity"]["result"]["backend"] == "osnet"
    assert len(sent) == 2
    for completion in sent:
        assert completion["outcome"] == "complete"
        assert len(completion["identity_descriptor"]["features"]) == 512
        assert completion["identity_descriptor"]["space_id"].startswith("osnet:")
    assert "features" not in json.dumps(saved_first)
    assert "identity_descriptor" not in json.dumps(saved_second)
    with repo.database.connection() as connection:
        assert {
            row[0]
            for row in connection.execute(
                "SELECT state FROM object_jobs WHERE stage='identity'"
            )
        } == {"complete"}
        assert (
            connection.execute("SELECT COUNT(*) FROM identity_gallery").fetchone()[0]
            == 2
        )
