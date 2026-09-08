from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from ai_cctv_core.contracts.objects import ObjectJobCompletion, LiveObjects
from ai_cctv_core.time import format_utc, utc_now
from server.services.data.app.config import Settings
from server.services.data.app.main import create_app
from server.services.preprocessing.app.objects import clip_detections, save_observation
from ai_cctv_core.processing.worker import ObjectWorker, safe_crop
from server.services.preprocessing.processors.identity import IdentityBlackBox
from server.services.analysis.processors import MetadataBlackBox
from server.services.preprocessing.processors.detection.contracts import DetectionResult


TOKENS = {
    name: name[0] * 40
    for name in ["external", "inference", "media", "recovery", "analysis"]
}
TOKENS["identity"] = "d" * 40


@pytest.fixture
def objects(tmp_path):
    settings = Settings(
        database_path=tmp_path / "db.sqlite",
        storage_root=tmp_path / "recordings",
        snapshot_root=tmp_path / "snapshots",
        backup_root=tmp_path / "backups",
        internal_token="",
        **{f"data_{name}_token": value for name, value in TOKENS.items()},
    )
    app = create_app(settings)
    with TestClient(app) as client:
        repo = app.state.repository
        for camera in ["cam-001", "cam-002"]:
            repo.create_camera(
                {"camera_id": camera, "name": camera, "stream_path": camera}
            )
        yield client, repo, settings


def appearance(
    client, camera="cam-001", person="7", session="a" * 32, crop="cam-001/crop.jpg"
):
    response = client.post(
        "/internal/v1/events",
        headers={"X-Internal-Token": TOKENS["inference"]},
        json={
            "camera_id": camera,
            "person_id": person,
            "event_type": "person_appeared",
            "occurred_at": format_utc(utc_now()),
            "object_observation": {
                "tracking_session_id": session,
                "bbox": [10, 20, 90, 100],
                "frame_width": 128,
                "frame_height": 128,
                "crop_path": crop,
            },
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_crop_matches_clipped_box_and_drawing_does_not_mutate_source(tmp_path):
    import cv2

    frame = np.full((60, 100, 3), 80, dtype=np.uint8)
    detections = clip_detections(
        [
            {"person_id": "7", "bbox": [-5, 10, 70, 80], "confidence": 0.9},
            {"person_id": "8", "bbox": [110, 0, 120, 5], "confidence": 0.5},
        ],
        100,
        60,
    )
    assert detections[0]["bbox"] == [0, 10, 70, 60]
    assert len(detections) == 1
    observation = save_observation(frame, detections[0], tmp_path, "cam-001", "a" * 32)
    assert cv2.imread(str(tmp_path / observation["crop_path"])).shape[:2] == (50, 70)
    assert not np.array_equal(
        cv2.imread(str(tmp_path / observation["annotated_snapshot_path"])), frame
    )
    assert np.all(frame == 80)


def test_identity_and_metadata_merge_independently_and_propagate_by_session(objects):
    client, repo, _ = objects
    event = appearance(client)
    identity = repo.claim_object_job("identity")
    analysis = repo.claim_object_job("analysis")
    assert repo.complete_object_job(
        "analysis",
        analysis["id"],
        ObjectJobCompletion(
            lease_id=analysis["lease_id"],
            outcome="complete",
            metadata={"attributes": {"coat": "blue"}},
        ),
    )
    assert repo.complete_object_job(
        "identity",
        identity["id"],
        ObjectJobCompletion(
            lease_id=identity["lease_id"],
            outcome="complete",
            global_person_id="global-1",
            metadata={"backend": "test"},
        ),
    )
    saved = repo.get_event(event["id"])
    assert saved["global_person_id"] == "global-1"
    assert saved["metadata"]["analysis"]["result"]["attributes"]["coat"] == "blue"
    assert saved["metadata"]["object"]["bbox"] == [10, 20, 90, 100]
    gone = repo.create_event(
        {
            "camera_id": "cam-001",
            "person_id": "7",
            "event_type": "person_disappeared",
            "occurred_at": format_utc(utc_now()),
            "metadata": {"tracking_session_id": "a" * 32},
        }
    )
    assert gone["global_person_id"] == "global-1"
    assert appearance(client, session="b" * 32)["global_person_id"] is None
    assert appearance(client, camera="cam-002")["global_person_id"] is None


def test_jobs_recover_from_worker_crash_and_ignore_stale_completion(objects):
    client, repo, _ = objects
    appearance(client)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(lambda _: repo.claim_object_job("identity"), range(2)))
    job = next(value for value in claims if value)
    assert sum(value is not None for value in claims) == 1
    with repo.database.transaction() as connection:
        connection.execute(
            "UPDATE object_jobs SET lease_until='2000-01-01T00:00:00Z' WHERE id=?",
            (job["id"],),
        )
    new = repo.claim_object_job("identity")
    assert new["lease_id"] != job["lease_id"]
    assert not repo.complete_object_job(
        "identity",
        job["id"],
        ObjectJobCompletion(
            lease_id=job["lease_id"], outcome="complete", global_person_id="wrong"
        ),
    )
    assert repo.complete_object_job(
        "identity",
        new["id"],
        ObjectJobCompletion(lease_id=new["lease_id"], outcome="unconfigured"),
    )
    assert repo.get_event(job["event_id"])["global_person_id"] is None
    assert repo.requeue_unconfigured_objects("identity") == 1
    assert repo.claim_object_job("identity") is not None


def test_internal_scopes_and_object_validation(objects):
    client, repo, _ = objects
    appearance(client)
    for scope in ["external", "inference", "analysis"]:
        assert (
            client.post(
                "/internal/v1/object-jobs/identity/claim",
                headers={"X-Internal-Token": TOKENS[scope]},
            ).status_code
            == 403
        )
    assert client.post("/internal/v1/object-jobs/identity/claim").status_code == 401
    result = client.post(
        "/internal/v1/object-jobs/analysis/claim",
        headers={"X-Internal-Token": TOKENS["analysis"]},
    ).json()["job"]
    forbidden = client.post(
        f"/internal/v1/object-jobs/analysis/{result['id']}/complete",
        headers={"X-Internal-Token": TOKENS["analysis"]},
        json={
            "lease_id": result["lease_id"],
            "outcome": "complete",
            "global_person_id": "no",
        },
    )
    assert forbidden.status_code == 409
    payload = LiveObjects(
        tracking_session_id="a" * 32,
        observed_at=utc_now(),
        frame_width=128,
        frame_height=128,
        objects=[{"person_id": "7", "bbox": [0, 0, 64, 128], "confidence": 0.8}],
    )
    for scope in ["identity", "analysis", "external"]:
        assert (
            client.put(
                "/internal/v1/cameras/cam-001/objects",
                headers={"X-Internal-Token": TOKENS[scope]},
                json=payload.model_dump(mode="json"),
            ).status_code
            == 403
        )
    invalid = payload.model_dump(mode="json")
    invalid["objects"][0]["bbox"] = [0, 0, 999, 128]
    assert (
        client.put(
            "/internal/v1/cameras/cam-001/objects",
            headers={"X-Internal-Token": TOKENS["inference"]},
            json=invalid,
        ).status_code
        == 422
    )


def test_live_objects_expire_and_reject_out_of_order_frames(objects):
    _, repo, _ = objects
    now = utc_now()
    frame = LiveObjects(
        tracking_session_id="a" * 32,
        observed_at=now,
        frame_width=128,
        frame_height=128,
        objects=[{"person_id": "7", "bbox": [0, 0, 64, 128], "confidence": 0.8}],
    )
    repo.put_live_objects("cam-001", frame)
    assert repo.get_live_objects("cam-001")["objects"][0]["person_id"] == "7"
    old = frame.model_copy(
        update={"observed_at": now - timedelta(seconds=1), "objects": []}
    )
    repo.put_live_objects("cam-001", old)
    assert len(repo.get_live_objects("cam-001")["objects"]) == 1
    with repo.database.transaction() as connection:
        connection.execute("UPDATE live_objects SET observed_at='2000-01-01T00:00:00Z'")
    assert repo.get_live_objects("cam-001") == {
        "camera_id": "cam-001",
        "objects": [],
        "stale": True,
    }


def test_metadata_blackbox_status_and_requeue_are_scoped(objects):
    client, repo, _ = objects
    event = appearance(client)
    job = repo.claim_object_job("analysis")
    result = MetadataBlackBox().process(job, None)
    completion = ObjectJobCompletion(lease_id=job["lease_id"], **result)
    assert repo.complete_object_job("analysis", job["id"], completion)
    saved = repo.get_event(event["id"])
    assert saved["metadata"]["analysis"]["status"] == "unconfigured"
    assert "attributes" not in saved["metadata"]["analysis"]["result"]
    assert saved["metadata"]["object"]["bbox"] == [10, 20, 90, 100]
    assert saved["global_person_id"] is None
    path = "/internal/v1/object-jobs/analysis/requeue-unconfigured"
    for scope in ("identity", "external", "inference"):
        assert (
            client.post(path, headers={"X-Internal-Token": TOKENS[scope]}).status_code
            == 403
        )
    response = client.post(path, headers={"X-Internal-Token": TOKENS["analysis"]})
    assert response.json() == {"requeued": 1}
    assert repo.claim_object_job("analysis")["id"] == job["id"]


@pytest.mark.asyncio
async def test_workers_report_both_blackboxes_as_unconfigured(tmp_path):
    crop = tmp_path / "crop.jpg"
    crop.write_bytes(b"opaque object image; blackboxes must not analyze it")
    job = {
        "id": 1,
        "event_id": 1,
        "camera_id": "cam-001",
        "person_id": "7",
        "lease_id": "b" * 32,
        "object_observation": {
            "tracking_session_id": "a" * 32,
            "bbox": [0, 0, 80, 80],
            "frame_width": 80,
            "frame_height": 80,
            "crop_path": "crop.jpg",
        },
    }
    completions = []

    def handler(request):
        import json

        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json={"job": job})
        completions.append(json.loads(request.content))
        return httpx.Response(200, json={"accepted": True})

    async with httpx.AsyncClient(
        base_url="http://data", transport=httpx.MockTransport(handler)
    ) as client:
        await ObjectWorker("identity", client, tmp_path, IdentityBlackBox()).once()
        await ObjectWorker("analysis", client, tmp_path, MetadataBlackBox()).once()
    assert completions[0]["outcome"] == "unconfigured"
    assert completions[0]["global_person_id"] is None
    assert completions[1]["outcome"] == "unconfigured"
    assert completions[1]["metadata"] == {"reason": "metadata_backend_not_implemented"}
    assert completions[1]["global_person_id"] is None
    with pytest.raises(ValueError):
        safe_crop(tmp_path / "nested", "../crop.jpg")


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["identity", "analysis"])
async def test_worker_reports_rejected_lease_and_recovers_on_next_job(objects, stage):
    client, repo, settings = objects
    crop = settings.snapshot_root / "cam-001/crop.jpg"
    crop.parent.mkdir(parents=True, exist_ok=True)
    crop.write_bytes(b"test crop")
    event = appearance(client)
    expire_lease = True

    class Processor:
        def process(self, job, crop_path):
            return {"outcome": "complete", "metadata": {"backend": "test"}}

    def handler(request):
        import json

        if request.url.path.endswith("/complete") and expire_lease:
            # 모델 처리 중 임대가 만료된 상황을 실제 Data API로 재현한다.
            with repo.database.transaction() as connection:
                connection.execute(
                    "UPDATE object_jobs SET lease_until='2000-01-01T00:00:00Z'"
                )
        result = client.post(
            "/internal/v1" + request.url.path,
            headers={"X-Internal-Token": TOKENS[stage]},
            json=json.loads(request.content) if request.content else None,
        )
        return httpx.Response(result.status_code, json=result.json())

    async with httpx.AsyncClient(
        base_url="http://data", transport=httpx.MockTransport(handler)
    ) as transport:
        worker = ObjectWorker(stage, transport, settings.snapshot_root, Processor())
        assert await worker.once()
        assert worker.last_outcome == "rejected"
        assert worker.last_error == "COMPLETION_NOT_ACCEPTED"
        assert not worker.stalled
        assert repo.get_event(event["id"])["metadata"][stage] == {"status": "pending"}

        expire_lease = False
        assert await worker.once()
        assert worker.last_outcome == "complete"
        assert worker.last_error is None
        assert repo.get_event(event["id"])["metadata"][stage]["status"] == "complete"


def test_existing_deployment_upgrade_keeps_existing_tokens(tmp_path, monkeypatch):
    from server.scripts.enable_object_processing import enable
    from server.scripts.doctor import read_deployment_env
    from server.scripts import generate_secrets

    monkeypatch.setattr(generate_secrets, "restrict_private_file", lambda _: None)
    secrets_root = tmp_path / "secrets"
    secrets_root.mkdir()
    data = secrets_root / "data.env"
    original = {
        f"DATA_{scope.upper()}_TOKEN": TOKENS[scope]
        for scope in ["external", "inference", "media", "recovery"]
    }
    data.write_text(
        "".join(f"{key}={value}\n" for key, value in original.items()), encoding="utf-8"
    )
    inference = secrets_root / "inference.env"
    inference_text = (
        f"DATA_INFERENCE_TOKEN={original['DATA_INFERENCE_TOKEN']}\n"
        "MEDIA_READ_USERNAME=existing-reader\n"
        f"MEDIA_READ_PASSWORD={'p' * 40}\n"
    )
    inference.write_text(inference_text, encoding="utf-8")
    env = tmp_path / "compose.env"
    preserved = (
        "# Keep deployment and push settings\n"
        "COMPOSE_PROJECT_NAME=existing-installation\n"
        "PUSH_ENABLED=true\n"
        "FIREBASE_PROJECT_ID=existing-project\n"
        'FIREBASE_SERVICE_ACCOUNT_FILE="C:/private keys/firebase.json"\n'
    )
    env.write_text(
        "DATA_SECRETS_FILE=./secrets/data.env\n" + preserved, encoding="utf-8"
    )
    enable(tmp_path, env)
    first = read_deployment_env(data)
    assert all(first[key] == value for key, value in original.items())
    assert read_deployment_env(secrets_root / "preprocessing.env") == {
        "DATA_INFERENCE_TOKEN": original["DATA_INFERENCE_TOKEN"],
        "DATA_IDENTITY_TOKEN": first["DATA_IDENTITY_TOKEN"],
        "MEDIA_READ_USERNAME": "existing-reader",
        "MEDIA_READ_PASSWORD": "p" * 40,
    }
    assert read_deployment_env(secrets_root / "analysis.env") == {
        "DATA_ANALYSIS_TOKEN": first["DATA_ANALYSIS_TOKEN"]
    }
    assert len(set(first.values())) == 6
    assert inference.read_text(encoding="utf-8") == inference_text
    assert not (secrets_root / "identity.env").exists()
    assert preserved in env.read_text(encoding="utf-8")
    for key, expected in (
        (
            "IDENTITY_PLUGIN",
            "server.services.preprocessing.processors.identity:IdentityBlackBox",
        ),
        ("ANALYSIS_PLUGIN", "server.services.analysis.processors:MetadataBlackBox"),
        (
            "DETECTION_PLUGIN",
            "server.services.preprocessing.processors.detection.yolo:YoloTracker",
        ),
    ):
        assert read_deployment_env(env)[key] == expected
    first_files = {path: path.read_bytes() for path in tmp_path.rglob("*.env")}
    enable(tmp_path, env)
    assert read_deployment_env(data) == first
    assert {path: path.read_bytes() for path in tmp_path.rglob("*.env")} == first_files


@pytest.fixture
def legacy_object_deployment(tmp_path, monkeypatch):
    """A seven-service installation with real split-token/media relationships."""
    from server.scripts import generate_secrets

    monkeypatch.setattr(generate_secrets, "restrict_private_file", lambda _: None)
    credentials = tmp_path / "credentials"
    credentials.mkdir()
    data = credentials / "data.env"
    tokens = {f"DATA_{name.upper()}_TOKEN": token for name, token in TOKENS.items()}
    data.write_text(
        "".join(f"{key}={value}\n" for key, value in tokens.items()), encoding="utf-8"
    )
    inference = credentials / "old-camera.env"
    inference.write_text(
        f"DATA_INFERENCE_TOKEN={tokens['DATA_INFERENCE_TOKEN']}\n"
        "MEDIA_READ_USERNAME=existing-reader\n"
        f"MEDIA_READ_PASSWORD={'p' * 40}\n",
        encoding="utf-8",
    )
    identity = credentials / "old-identity.env"
    identity.write_text(
        f"DATA_IDENTITY_TOKEN={tokens['DATA_IDENTITY_TOKEN']}\n", encoding="utf-8"
    )
    analysis = credentials / "analysis.env"
    analysis.write_text(
        f"DATA_ANALYSIS_TOKEN={tokens['DATA_ANALYSIS_TOKEN']}\n", encoding="utf-8"
    )
    env = tmp_path / "compose.env"
    env.write_text(
        "DATA_SECRETS_FILE=./credentials/data.env\n"
        "INFERENCE_SECRETS_FILE=./credentials/old-camera.env\n"
        "IDENTITY_SECRETS_FILE=./credentials/old-identity.env\n"
        "ANALYSIS_SECRETS_FILE=./credentials/analysis.env\n"
        "IDENTITY_PLUGIN=app.plugins:IdentityBlackBox\n"
        "ANALYSIS_PLUGIN=app.plugins:MetadataBlackBox\n"
        "DETECTION_PLUGIN=custom.detector:Detector\n"
        "PUSH_ENABLED=true\n",
        encoding="utf-8",
    )
    return tmp_path, env, data, inference, identity, analysis, tokens


def test_existing_deployment_upgrade_migrates_custom_legacy_paths(
    legacy_object_deployment,
):
    from server.scripts.doctor import read_deployment_env
    from server.scripts.enable_object_processing import enable

    root, env, data, inference, identity, analysis, tokens = legacy_object_deployment
    before = {path: path.read_bytes() for path in (data, inference, identity, analysis)}
    enable(root, env)
    assert read_deployment_env(data) == tokens
    assert inference.read_bytes() == before[inference]
    assert identity.read_bytes() == before[identity]
    config = read_deployment_env(env)
    assert "INFERENCE_SECRETS_FILE" not in config
    assert "IDENTITY_SECRETS_FILE" not in config
    assert config["PREPROCESSING_SECRETS_FILE"] == str(
        data.with_name("preprocessing.env")
    )
    assert config["ANALYSIS_SECRETS_FILE"] == str(analysis)
    assert (
        config["IDENTITY_PLUGIN"]
        == "server.services.preprocessing.processors.identity:IdentityBlackBox"
    )
    assert (
        config["ANALYSIS_PLUGIN"]
        == "server.services.analysis.processors:MetadataBlackBox"
    )
    assert config["DETECTION_PLUGIN"] == "custom.detector:Detector"
    assert config["PUSH_ENABLED"] == "true"
    first_files = {path: path.read_bytes() for path in root.rglob("*.env")}
    enable(root, env)
    assert {path: path.read_bytes() for path in root.rglob("*.env")} == first_files


@pytest.mark.parametrize(
    "invalid",
    ["conflicting_identity", "duplicate_token", "missing_media", "colliding_output"],
)
def test_existing_deployment_upgrade_validates_before_writing(
    legacy_object_deployment, invalid
):
    from server.scripts.enable_object_processing import enable

    root, env, data, inference, identity, analysis, tokens = legacy_object_deployment
    if invalid == "conflicting_identity":
        identity.write_text(f"DATA_IDENTITY_TOKEN={'z' * 40}\n", encoding="utf-8")
    elif invalid == "duplicate_token":
        data.write_text(
            data.read_text(encoding="utf-8").replace(
                tokens["DATA_IDENTITY_TOKEN"], tokens["DATA_INFERENCE_TOKEN"]
            ),
            encoding="utf-8",
        )
        identity.write_text(
            f"DATA_IDENTITY_TOKEN={tokens['DATA_INFERENCE_TOKEN']}\n", encoding="utf-8"
        )
    elif invalid == "missing_media":
        inference.write_text(
            f"DATA_INFERENCE_TOKEN={tokens['DATA_INFERENCE_TOKEN']}\n", encoding="utf-8"
        )
    else:
        env.write_text(
            env.read_text(encoding="utf-8")
            + "PREPROCESSING_SECRETS_FILE=./credentials/old-camera.env\n",
            encoding="utf-8",
        )
    before = {path: path.read_bytes() for path in root.rglob("*.env")}
    with pytest.raises(ValueError):
        enable(root, env)
    assert {path: path.read_bytes() for path in root.rglob("*.env")} == before


def test_existing_deployment_upgrade_reuses_tokens_missing_from_data(
    legacy_object_deployment,
):
    from server.scripts.doctor import read_deployment_env
    from server.scripts.enable_object_processing import enable

    root, env, data, _inference, _identity, _analysis, tokens = legacy_object_deployment
    data.write_text(
        "".join(
            f"{key}={value}\n"
            for key, value in tokens.items()
            if key not in {"DATA_IDENTITY_TOKEN", "DATA_ANALYSIS_TOKEN"}
        ),
        encoding="utf-8",
    )
    enable(root, env)
    assert read_deployment_env(data) == tokens


def test_compose_has_six_services_and_processing_has_no_database_mount():
    from pathlib import Path
    import yaml

    compose = yaml.safe_load(Path("server/compose.yml").read_text(encoding="utf-8"))
    assert set(compose["services"]) == {
        "data",
        "external",
        "preprocessing",
        "analysis",
        "mediamtx",
        "nginx",
    }
    for name in ["preprocessing", "analysis"]:
        service = compose["services"][name]
        assert "ports" not in service
        volumes = {volume["target"]: volume for volume in service["volumes"]}
        assert "/data/database" not in volumes
        assert volumes["/models"]["read_only"]
        assert volumes["/snapshots"].get("read_only", False) == (name == "analysis")
        assert service["build"]["dockerfile"] == f"server/services/{name}/Dockerfile"
    assert all(
        volume["read_only"] for volume in compose["services"]["analysis"]["volumes"]
    )

    # 폴더를 옮긴 뒤에도 이미지 빌드와 설정 마운트가 실제 파일을 가리켜야 한다.
    server_root = Path("server").resolve()
    for name, service in compose["services"].items():
        service_root = server_root / "services" / name
        assert service_root.is_dir()
        if "build" in service:
            context = (server_root / service["build"]["context"]).resolve()
            dockerfile = (context / service["build"]["dockerfile"]).resolve()
            assert dockerfile == service_root / "Dockerfile"
            assert dockerfile.is_file()
        for volume in service.get("volumes", []):
            if not volume["source"].startswith("${"):
                source = (server_root / volume["source"]).resolve()
                assert source.is_relative_to(service_root)
                assert source.is_file()


def test_camera_worker_passes_real_crops_and_boxes_into_data_jobs(objects, monkeypatch):
    import cv2
    from server.services.preprocessing.app.pipeline import CameraWorker
    from server.services.preprocessing.app.settings import Settings as InferenceSettings

    client, repo, settings = objects

    class DataAdapter:
        def create_event(self, payload):
            result = client.post(
                "/internal/v1/events",
                headers={"X-Internal-Token": TOKENS["inference"]},
                json=payload,
            )
            assert result.status_code == 201, result.text

        def set_camera_status(self, *args):
            pass

    class Tracker:
        def process(self, frame):
            return DetectionResult(
                objects=[
                    {"person_id": "7", "bbox": [10, 20, 90, 100], "confidence": 0.9}
                ]
            )

        def reset(self):
            pass

    inference_settings = InferenceSettings(
        data_service_url="http://data",
        internal_service_token=TOKENS["inference"],
        rtsp_base_url="rtsp://media",
        media_read_username="reader",
        media_read_password="x" * 40,
        snapshots_root=settings.snapshot_root,
        model_path=settings.snapshot_root / "model.pt",
        device="cpu",
        confidence=0.4,
        analysis_fps=5,
        disappear_seconds=3,
        refresh_seconds=15,
        inference_enabled=True,
    )
    worker = CameraWorker(
        {"camera_id": "cam-001"},
        inference_settings,
        DataAdapter(),
        tracker_factory=lambda *_: Tracker(),
    )

    class Capture:
        emitted = False

        def isOpened(self):
            return True

        def read(self):
            if self.emitted:
                worker.stop()
                return False, None
            self.emitted = True
            return True, np.zeros((128, 128, 3), dtype=np.uint8)

        def release(self):
            pass

    monkeypatch.setattr(cv2, "VideoCapture", lambda *_: Capture())
    worker._run()
    job = repo.claim_object_job("analysis")
    assert job["person_id"] == "7"
    crop = settings.snapshot_root / job["object_observation"]["crop_path"]
    assert crop.is_file()
    assert job["object_observation"]["bbox"] == [10, 20, 90, 100]
