"""실제 SQLite로 비공개 특징 저장, 임대 원자성, 보수적인 인물 연결을 검증한다."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import math
import sqlite3
import shutil

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from ai_cctv_core.contracts.objects import IdentityDescriptor, ObjectJobCompletion
from ai_cctv_core.time import format_utc, utc_now
from server.services.data.app.database.connection import Database
from server.services.data.app.database.repositories import DataRepository
from server.services.data.app.database.repositories import identity as gallery
from server.services.data.app.config import Settings
from server.services.data.app.main import create_app
from server.services.data.app.schemas import EventCreate


@pytest.fixture
def repository(tmp_path):
    repo = DataRepository(Database(tmp_path / "gallery.db"))
    repo.initialize()
    for camera in ("cam-001", "cam-002", "cam-003"):
        repo.create_camera({"camera_id": camera, "name": camera, "stream_path": camera})
    return repo


def descriptor(axis=0, *, space="test-appearance-v1", negative=False):
    features = [0.0] * 16
    features[axis] = -1.0 if negative else 1.0
    return IdentityDescriptor(space_id=space, features=features)


def appearance(repo, camera="cam-001", person="1", session="a" * 32, at=None, **extra):
    observation = {
        "tracking_session_id": session,
        "bbox": [0, 0, 16, 32],
        "frame_width": 16,
        "frame_height": 32,
        "crop_path": f"{camera}/crop.jpg",
    }
    return repo.create_event(
        {
            "camera_id": camera,
            "event_type": "person_appeared",
            "person_id": person,
            "occurred_at": format_utc(at or utc_now()),
            "object_observation": observation,
            "metadata": {"object": observation, "tracking_session_id": session},
            **extra,
        }
    )


def finish(repo, event, features=None):
    job = repo.claim_object_job("identity")
    assert job["event_id"] == event["id"]
    completion = ObjectJobCompletion(
        lease_id=job["lease_id"],
        outcome="complete",
        identity_descriptor=features or descriptor(),
        metadata={"backend": "fixture"},
    )
    assert repo.complete_object_job("identity", job["id"], completion)
    return repo.get_event(event["id"])


def stored_rows(repo, table="identity_gallery"):
    with repo.database.connection() as connection:
        return [dict(row) for row in connection.execute(f"SELECT * FROM {table}")]


def decision(event):
    return event["metadata"]["identity"]["result"]["match"]["decision"]


def test_cross_camera_matching_persists_without_exposing_features(repository):
    first = finish(repository, appearance(repository))
    # 객체 작업자와 저장소 객체를 교체해도 인물 연결의 기준은 SQLite에 남는다.
    restarted = DataRepository(Database(repository.database.path))
    restarted.initialize()
    second = finish(restarted, appearance(restarted, camera="cam-002"))
    assert first["global_person_id"] == second["global_person_id"]
    assert decision(first) == "new"
    assert decision(second) == "matched"
    result = second["metadata"]["identity"]["result"]
    assert result == {
        "backend": "fixture",
        "match": {"method": "appearance", "decision": "matched", "similarity": 1.0},
    }
    assert "features" not in json.dumps(second)
    assert "identity_descriptor" not in json.dumps(second)
    assert len(stored_rows(restarted)) == 2


@pytest.mark.parametrize(
    "variant", ["different", "space", "old", "same_camera", "ambiguous"]
)
def test_incompatible_or_ambiguous_candidates_create_new_id(repository, variant):
    observed = utc_now()
    first = finish(repository, appearance(repository, at=observed))
    features = descriptor()
    camera = "cam-002"
    when = observed
    if variant == "different":
        features = descriptor(axis=1)
    elif variant == "space":
        features = descriptor(space="other-model-v1")
    elif variant == "old":
        when += timedelta(seconds=1801)
    elif variant == "same_camera":
        camera = "cam-001"
    elif variant == "ambiguous":
        other = finish(repository, appearance(repository, person="2", at=observed))
        assert other["global_person_id"] != first["global_person_id"]
    result = finish(
        repository, appearance(repository, camera=camera, person="3", at=when), features
    )
    assert result["global_person_id"] != first["global_person_id"]
    assert decision(result) == "new"
    if variant == "ambiguous":
        assert result["global_person_id"] != other["global_person_id"]
    if variant == "old":
        assert len(stored_rows(repository)) == 1


def test_same_identity_samples_do_not_count_as_second_best_candidate(repository):
    first = finish(repository, appearance(repository))
    finish(repository, appearance(repository, camera="cam-002"))
    third = finish(repository, appearance(repository, camera="cam-003"))
    assert third["global_person_id"] == first["global_person_id"]
    assert decision(third) == "matched"


def test_same_camera_conflict_blocks_identity_even_with_other_camera_sample(repository):
    first = finish(repository, appearance(repository))
    finish(repository, appearance(repository, camera="cam-002"))
    result = finish(repository, appearance(repository, person="another-track"))
    assert result["global_person_id"] != first["global_person_id"]


def test_existing_track_wins_and_older_descriptor_does_not_replace_newer_sample(
    repository,
):
    observed = utc_now()
    first = finish(repository, appearance(repository, at=observed))
    second = finish(
        repository,
        appearance(repository, at=observed + timedelta(seconds=5)),
        descriptor(1),
    )
    third = finish(
        repository,
        appearance(repository, at=observed - timedelta(seconds=5)),
        descriptor(2),
    )
    assert {
        first["global_person_id"],
        second["global_person_id"],
        third["global_person_id"],
    } == {first["global_person_id"]}
    assert decision(second) == decision(third) == "existing_track"
    rows = stored_rows(repository)
    assert len(rows) == 1
    assert json.loads(rows[0]["features_json"]) == descriptor(1).features


def test_stale_lease_rejection_cannot_change_gallery_or_links(repository):
    finish(repository, appearance(repository))
    event = appearance(repository, camera="cam-002")
    stale = repository.claim_object_job("identity")
    with repository.database.transaction() as connection:
        connection.execute(
            "UPDATE object_jobs SET lease_until=? WHERE id=?",
            (format_utc(utc_now() - timedelta(seconds=1)), stale["id"]),
        )
    replacement = repository.claim_object_job("identity")
    before = stored_rows(repository), stored_rows(repository, "person_identity_links")
    assert not repository.complete_object_job(
        "identity",
        stale["id"],
        ObjectJobCompletion(
            lease_id=stale["lease_id"],
            outcome="complete",
            identity_descriptor=descriptor(1),
        ),
    )
    assert (
        stored_rows(repository),
        stored_rows(repository, "person_identity_links"),
    ) == before
    assert repository.get_event(event["id"])["global_person_id"] is None
    assert repository.complete_object_job(
        "identity",
        replacement["id"],
        ObjectJobCompletion(
            lease_id=replacement["lease_id"],
            outcome="complete",
            identity_descriptor=descriptor(),
        ),
    )


def test_concurrent_completion_serializes_assignment_and_duplicate_is_noop(repository):
    first = appearance(repository)
    second = appearance(repository, camera="cam-002")
    claims = [repository.claim_object_job("identity") for _ in range(2)]

    def complete(job):
        return repository.complete_object_job(
            "identity",
            job["id"],
            ObjectJobCompletion(
                lease_id=job["lease_id"],
                outcome="complete",
                identity_descriptor=descriptor(),
            ),
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(complete, claims)) == [True, True]
    assert (
        repository.get_event(first["id"])["global_person_id"]
        == repository.get_event(second["id"])["global_person_id"]
    )
    before = stored_rows(repository)
    assert complete(claims[0]) is False
    assert stored_rows(repository) == before


def test_failure_after_gallery_write_rolls_back_every_identity_change(repository):
    event = appearance(repository)
    job = repository.claim_object_job("identity")
    with repository.database.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER reject_completion BEFORE UPDATE ON object_jobs WHEN NEW.state='complete' BEGIN SELECT RAISE(ABORT,'completion failed'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="completion failed"):
        repository.complete_object_job(
            "identity",
            job["id"],
            ObjectJobCompletion(
                lease_id=job["lease_id"],
                outcome="complete",
                identity_descriptor=descriptor(),
            ),
        )
    assert stored_rows(repository) == []
    assert stored_rows(repository, "person_identity_links") == []
    assert repository.get_event(event["id"])["global_person_id"] is None
    assert stored_rows(repository, "object_jobs")[0]["state"] == "running"


def test_analysis_cannot_submit_identity_descriptor(repository):
    appearance(repository)
    job = repository.claim_object_job("analysis")
    with pytest.raises(ValueError, match="Only identity"):
        repository.complete_object_job(
            "analysis",
            job["id"],
            ObjectJobCompletion(
                lease_id=job["lease_id"],
                outcome="complete",
                identity_descriptor=descriptor(),
            ),
        )
    assert stored_rows(repository) == []
    assert stored_rows(repository, "person_identity_links") == []


def test_gallery_retention_is_bounded_and_link_survives_eviction(
    repository, monkeypatch
):
    monkeypatch.setattr(gallery, "MAX_GALLERY_SAMPLES", 2)
    first = finish(repository, appearance(repository))
    finish(repository, appearance(repository, person="2"), descriptor(1))
    finish(repository, appearance(repository, person="3"), descriptor(2))
    assert len(stored_rows(repository)) == 2
    again = finish(repository, appearance(repository), descriptor(3))
    assert again["global_person_id"] == first["global_person_id"]
    assert decision(again) == "existing_track"
    assert len(stored_rows(repository)) == 2


@pytest.mark.parametrize(
    "features",
    [
        [1.0] * 15,
        [0.0] * 16,
        [1.0] * 2049,
        [math.nan] * 16,
        [math.inf] * 16,
        [0.5] * 16,
    ],
)
def test_invalid_descriptors_are_rejected(features):
    with pytest.raises(ValidationError):
        IdentityDescriptor(space_id="test", features=features)


def test_descriptor_contract_accepts_signed_unit_vector_and_rejects_conflicting_results():
    signed = descriptor(negative=True)
    assert signed.features[0] == -1.0
    for outcome in ("retry", "failed", "unconfigured"):
        with pytest.raises(ValidationError, match="completed identity"):
            ObjectJobCompletion(
                lease_id="a" * 32, outcome=outcome, identity_descriptor=signed
            )
    with pytest.raises(ValidationError, match="either"):
        ObjectJobCompletion(
            lease_id="a" * 32,
            outcome="complete",
            identity_descriptor=signed,
            global_person_id="custom",
        )


@pytest.mark.parametrize("source_id", ["bad", "A" * 32, "a" * 31, "g" * 32])
def test_source_event_id_is_32_lowercase_hex(source_id):
    with pytest.raises(ValidationError):
        EventCreate(
            camera_id="cam-001",
            event_type="person_appeared",
            occurred_at=utc_now(),
            source_event_id=source_id,
        )


def test_source_event_retries_enqueue_push_and_object_jobs_only_once_per_camera(
    repository,
):
    user = repository.create_user(
        {"username": "operator", "role": "admin", "password_hash": "test-only"}
    )
    repository.issue_refresh_token(
        {
            "user_id": user["id"],
            "jti": "session",
            "token_hash": "test-hash",
            "expires_at": format_utc(utc_now() + timedelta(hours=1)),
        }
    )
    repository.put_mobile_device(
        {
            "device_id": "a" * 32,
            "user_id": user["id"],
            "refresh_jti": "session",
            "token": "test-device-token-0123456789",
            "platform": "android",
            "enabled": True,
            "event_types": None,
        }
    )
    source_id = "b" * 32
    with ThreadPoolExecutor(max_workers=3) as pool:
        events = list(
            pool.map(
                lambda _: appearance(repository, source_event_id=source_id), range(3)
            )
        )
    assert len({event["id"] for event in events}) == 1
    assert events[0]["source_event_id"] == source_id
    assert len(stored_rows(repository, "events")) == 1
    assert len(stored_rows(repository, "object_jobs")) == 2
    assert len(stored_rows(repository, "push_deliveries")) == 1
    other = appearance(repository, camera="cam-002", source_event_id=source_id)
    assert other["id"] != events[0]["id"]
    assert len(stored_rows(repository, "object_jobs")) == 4
    assert len(stored_rows(repository, "push_deliveries")) == 2
    edge = appearance(repository, edge_event_id="edge-stable-id")
    assert appearance(repository, edge_event_id="edge-stable-id")["id"] == edge["id"]


def test_descriptor_completion_api_enforces_stage_and_token_scope(repository, tmp_path):
    tokens = {
        name: name[0] * 40
        for name in ("external", "inference", "media", "recovery", "analysis")
    }
    tokens["identity"] = "d" * 40
    settings = Settings(
        database_path=repository.database.path,
        storage_root=tmp_path / "recordings",
        snapshot_root=tmp_path / "snapshots",
        backup_root=tmp_path / "backups",
        internal_token="",
        **{f"data_{name}_token": value for name, value in tokens.items()},
    )
    event = appearance(repository)
    identity_job = repository.claim_object_job("identity")
    analysis_job = repository.claim_object_job("analysis")
    payload = {
        "lease_id": identity_job["lease_id"],
        "outcome": "complete",
        "identity_descriptor": descriptor().model_dump(),
    }
    base = "/internal/v1/object-jobs"
    with TestClient(create_app(settings)) as client:
        wrong_scope = client.post(
            f"{base}/identity/{identity_job['id']}/complete",
            json=payload,
            headers={"X-Internal-Token": tokens["analysis"]},
        )
        assert wrong_scope.status_code == 403
        analysis_payload = {**payload, "lease_id": analysis_job["lease_id"]}
        wrong_stage = client.post(
            f"{base}/analysis/{analysis_job['id']}/complete",
            json=analysis_payload,
            headers={"X-Internal-Token": tokens["analysis"]},
        )
        assert wrong_stage.status_code == 409
        assert stored_rows(repository) == []
        accepted = client.post(
            f"{base}/identity/{identity_job['id']}/complete",
            json=payload,
            headers={"X-Internal-Token": tokens["identity"]},
        )
        assert accepted.status_code == 200
        assert accepted.json() == {"accepted": True}
    assert repository.get_event(event["id"])["global_person_id"] is not None


def test_migration_preserves_existing_edge_events_and_is_repeatable(tmp_path):
    legacy_dir = tmp_path / "legacy-migrations"
    legacy_dir.mkdir()
    database = Database(tmp_path / "upgrade.db")
    for migration in database.migrations_dir.glob("*.sql"):
        if migration.name < "007":
            shutil.copyfile(migration, legacy_dir / migration.name)
    legacy = DataRepository(Database(database.path, migrations_dir=legacy_dir))
    legacy.initialize()
    legacy.create_camera(
        {"camera_id": "cam-001", "name": "Entrance", "stream_path": "cam-001"}
    )
    stamp = format_utc(utc_now())
    with legacy.database.transaction() as connection:
        connection.execute(
            "INSERT INTO events(camera_id,event_type,occurred_at,created_at,edge_event_id) VALUES (?,?,?,?,?)",
            ("cam-001", "motion_detected", stamp, stamp, "original"),
        )
    updated = DataRepository(database)
    updated.initialize()
    updated.initialize()
    existing = updated.get_event(1)
    assert existing["edge_event_id"] == "original"
    assert existing["source_event_id"] is None
    assert stored_rows(updated) == []
