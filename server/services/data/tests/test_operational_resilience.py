"""잠금·작업 중단·오래된 데이터·동시 track의 운영 회귀를 실제 SQLite로 검증한다."""

import asyncio
from dataclasses import replace
from datetime import timedelta
import json
import os
import sqlite3
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ai_cctv_core.contracts.objects import LiveObjects, ObjectJobCompletion
from ai_cctv_core.time import format_utc, utc_now
from server.services.data.app.api.events import _event_values
from server.services.data.app.config import Settings
from server.services.data.app.database.connection import Database
from server.services.data.app.database.repositories import DataRepository
from server.services.data.app.main import create_app
from server.services.data.app.schemas import EventCreate, RetentionRequest
from server.services.data.app.storage.protection import read_snapshot_protection
from server.services.data.app.storage.protection import SnapshotProtectionReader
from server.services.data.app.storage.retention import retention_cleanup, storage_usage
from server.services.data.app.workers import recovery
from server.services.data.app.workers.supervision import WorkerStatus, supervise
from server.services.data.tests.test_identity_gallery import appearance, finish


@pytest.fixture
def runtime(tmp_path):
    settings = Settings(
        database_path=tmp_path / "database" / "data.db",
        storage_root=tmp_path / "recordings",
        snapshot_root=tmp_path / "snapshots",
        backup_root=tmp_path / "backups",
        internal_token="t" * 32,
        busy_timeout_ms=20,
        recovery_poll_interval_seconds=0.01,
    )
    settings.prepare_directories()
    repo = DataRepository(Database(settings.database_path, busy_timeout_ms=20))
    repo.initialize()
    repo.create_camera(
        {"camera_id": "cam-001", "name": "camera", "stream_path": "cam-001"}
    )
    yield repo, settings
    scanner = getattr(repo, "snapshot_scanner", None)
    if scanner is not None:
        scanner.close()


def manifest(settings, *, paths=(), events=(), generated_at=None):
    (settings.snapshot_root / ".pending-observations.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "generated_at": generated_at or format_utc(utc_now()),
                "paths": list(paths),
                "events": list(events),
            }
        ),
        encoding="utf-8",
    )


async def wait_until(predicate):
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("background operation did not recover")


@pytest.mark.asyncio
async def test_recovery_survives_real_sqlite_claim_lock(runtime):
    repo, settings = runtime
    state = WorkerStatus(alive=True)
    writer = repo.database.connect()
    writer.execute("BEGIN IMMEDIATE")
    task = asyncio.create_task(recovery.recover_outages(repo, settings, state))
    try:
        await wait_until(lambda: state.status == "error")
        assert state.last_error == "DATABASE_UNAVAILABLE"
        assert not task.done()
        writer.rollback()
        await wait_until(lambda: state.status == "ok")
        assert state.last_success_at is not None
        assert not task.done()
    finally:
        writer.close()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_recovery_retries_failure_record_before_claiming_again(
    runtime, monkeypatch
):
    _, settings = runtime
    calls = []
    job = {"id": 7, "attempt_count": 1, "max_attempts": 3, "revision": 4}

    class Repository:
        def claim_due_recovery_job(self):
            calls.append("claim")
            return job if calls.count("claim") == 1 else None

        def update_recovery_job(self, job_id, **values):
            calls.append("record")
            assert job_id == 7 and values["expected_revision"] == 4
            if calls.count("record") < 3:
                raise sqlite3.OperationalError("database is locked")
            return job

    def fail(*_args, **_kwargs):
        raise OSError("temporary filesystem failure")

    monkeypatch.setattr(recovery, "execute_recovery", fail)
    task = asyncio.create_task(recovery.recover_outages(Repository(), settings))
    try:
        await wait_until(lambda: calls.count("claim") >= 2)
        assert calls[:5] == ["claim", "record", "record", "record", "claim"]
        assert not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_supervisor_restarts_unexpected_exit_and_reports_shutdown():
    state = WorkerStatus()
    entered = 0

    async def operation():
        nonlocal entered
        entered += 1
        if entered == 1:
            raise RuntimeError("injected worker exit")
        state.succeeded()
        await asyncio.Event().wait()

    task = asyncio.create_task(supervise(operation, state, retry_seconds=0.01))
    await wait_until(lambda: entered == 2)
    assert state.alive and state.status == "ok"
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not state.alive


def test_health_exposes_dead_worker_and_separate_volume_capacity(runtime, monkeypatch):
    repo, settings = runtime
    original = __import__("shutil").disk_usage

    def usage(path):
        if path == settings.snapshot_root:
            return SimpleNamespace(total=1000, used=990, free=10)
        return original(path)

    monkeypatch.setattr(
        "server.services.data.app.storage.retention.shutil.disk_usage", usage
    )
    assert storage_usage(settings)["status"] == "warning"
    with TestClient(create_app(settings=settings, repository=repo)) as client:
        task = client.app.state.worker_tasks["recovery"]
        client.portal.call(task.cancel)
        response = client.get("/health/ready")
        assert response.status_code == 503
        body = response.json()
        assert body["workers"]["recovery"]["alive"] is False
        assert body["queues"]["identity"]["pending"] == 0
        assert body["queue_metrics"]["identity"]["oldest_pending_seconds"] is None
        assert body["storage"]["volumes"]["snapshots"]["free_percent"] == 1.0
        assert body["retention"]["reason"] == "OUTBOX_MANIFEST_MISSING"


def old_event(repo, settings, person, *, age_days=40):
    old = utc_now() - timedelta(days=age_days)
    crop = f"{person}_crop.jpg"
    path = settings.snapshot_root / crop
    path.write_bytes(b"crop")
    os.utime(path, (old.timestamp(), old.timestamp()))
    event = repo.create_event(
        _event_values(
            EventCreate(
                camera_id="cam-001",
                event_type="person_appeared",
                occurred_at=old,
                person_id=person,
                source_event_id=(f"{int(person):032x}"),
                snapshot_path=crop,
                object_observation={
                    "tracking_session_id": "a" * 32,
                    "bbox": [0, 0, 10, 10],
                    "frame_width": 10,
                    "frame_height": 10,
                    "crop_path": crop,
                },
            ),
            settings,
        )
    )
    with repo.database.transaction() as connection:
        connection.execute(
            "UPDATE events SET created_at=? WHERE id=?", (format_utc(old), event["id"])
        )
        connection.execute(
            "UPDATE object_jobs SET state='complete',created_at=?,updated_at=? WHERE event_id=?",
            (format_utc(old), format_utc(old), event["id"]),
        )
    return event, path


def cleanup(repo, settings):
    return retention_cleanup(
        repo, settings, RetentionRequest(retention_days=7, dry_run=False)
    )


def test_retention_expires_abandoned_jobs_but_keeps_live_leases_and_recent_failures(
    runtime,
):
    repo, settings = runtime
    manifest(settings)
    pending, pending_crop = old_event(repo, settings, "1")
    expired, expired_crop = old_event(repo, settings, "2")
    active, active_crop = old_event(repo, settings, "3")
    complete, complete_crop = old_event(repo, settings, "4")
    with repo.database.transaction() as connection:
        connection.execute(
            "UPDATE object_jobs SET state='pending' WHERE event_id=? AND stage='identity'",
            (pending["id"],),
        )
        connection.execute(
            "UPDATE object_jobs SET state='running',lease_until=? WHERE event_id=? AND stage='identity'",
            (format_utc(utc_now() - timedelta(minutes=1)), expired["id"]),
        )
        connection.execute(
            "UPDATE object_jobs SET state='running',lease_until=? WHERE event_id=? AND stage='identity'",
            (format_utc(utc_now() + timedelta(minutes=1)), active["id"]),
        )
    result = cleanup(repo, settings)
    assert result["expired_object_jobs"] == 2
    for event in (pending, expired):
        metadata = repo.get_event(event["id"])["metadata"]["identity"]
        assert metadata["status"] == "failed"
        assert metadata["result"]["error_code"] == "OBJECT_RETENTION_EXPIRED"
    assert repo.get_event(active["id"])["metadata"]["identity"]["status"] == "running"
    assert repo.get_event(complete["id"]) is None and not complete_crop.exists()
    assert pending_crop.exists() and expired_crop.exists() and active_crop.exists()


@pytest.mark.parametrize("invalid", ["missing", "stale", "invalid"])
def test_retention_defers_without_trustworthy_outbox_manifest(runtime, invalid):
    repo, settings = runtime
    event, path = old_event(repo, settings, "1")
    if invalid == "stale":
        manifest(settings, generated_at=format_utc(utc_now() - timedelta(minutes=3)))
    elif invalid == "invalid":
        (settings.snapshot_root / ".pending-observations.json").write_text("{}")
    result = cleanup(repo, settings)
    assert result["snapshot_protection"]["status"] == "deferred"
    assert repo.get_event(event["id"]) is not None and path.exists()
    manifest(settings)
    cleanup(repo, settings)
    assert repo.get_event(event["id"]) is None and not path.exists()


def test_retention_preserves_outbox_event_keys_paths_and_new_unregistered_crops(
    runtime,
):
    repo, settings = runtime
    event, path = old_event(repo, settings, "1")
    orphan = settings.snapshot_root / "outbox.jpg"
    orphan.write_bytes(b"waiting for Data")
    old = (utc_now() - timedelta(days=40)).timestamp()
    os.utime(orphan, (old, old))
    recent = settings.snapshot_root / "not-registered-yet.jpg"
    recent.write_bytes(b"new observation")
    manifest(
        settings,
        paths=["outbox.jpg"],
        events=[{"camera_id": "cam-001", "source_event_id": event["source_event_id"]}],
    )
    cleanup(repo, settings)
    assert repo.get_event(event["id"]) is not None
    assert path.exists() and orphan.exists() and recent.exists()
    assert read_snapshot_protection(settings).ready


def test_snapshot_cleanup_never_deletes_outbox_databases_or_unrecognized_files(runtime):
    repo, settings = runtime
    manifest(settings)
    old = (utc_now() - timedelta(days=40)).timestamp()
    protected_names = (
        ".event-outbox.sqlite3",
        ".event-outbox.sqlite3-wal",
        ".event-outbox.sqlite3-shm",
        "operator-notes.txt",
        "model.onnx",
    )
    for name in (
        *protected_names,
        "old.jpg",
        ".snapshot-orphan",
        ".outbox-protection-orphan",
    ):
        target = settings.snapshot_root / name
        target.write_bytes(b"persistent state")
        os.utime(target, (old, old))
    result = cleanup(repo, settings)
    assert result["snapshots_deleted"] == 3
    assert all((settings.snapshot_root / name).exists() for name in protected_names)
    assert (settings.snapshot_root / ".pending-observations.json").exists()


def test_snapshot_scan_is_bounded_and_resumes_past_recent_files_after_restart(
    runtime, monkeypatch
):
    repo, settings = runtime
    manifest(settings)
    monkeypatch.setattr(
        "server.services.data.app.storage.snapshot_scan.MAX_SCAN_ENTRIES", 5
    )
    for index in range(12):
        (settings.snapshot_root / f"recent-{index:02}.jpg").write_bytes(b"recent")
    target = settings.snapshot_root / "zz-old.jpg"
    target.write_bytes(b"orphan")
    old = (utc_now() - timedelta(days=40)).timestamp()
    os.utime(target, (old, old))
    first = cleanup(repo, settings)
    assert first["snapshot_scan"]["examined"] <= 5
    repo.snapshot_scanner.close()
    restarted = DataRepository(repo.database)
    for _ in range(8):
        result = cleanup(restarted, settings)
        assert result["snapshot_scan"]["examined"] <= 5
        if not target.exists():
            break
    assert not target.exists()
    assert len(list(settings.snapshot_root.glob("recent-*.jpg"))) == 12
    restarted.snapshot_scanner.close()


def test_retention_purges_expired_token_link_and_recording_history_but_keeps_writing(
    runtime,
):
    repo, settings = runtime
    manifest(settings)
    event, _ = old_event(repo, settings, "1")
    old = format_utc(utc_now() - timedelta(days=40))
    user = repo.create_user(
        {"username": "history", "password_hash": "hash", "role": "viewer"}
    )
    repo.issue_refresh_token(
        {
            "user_id": user["id"],
            "jti": "expired",
            "token_hash": "hash",
            "expires_at": old,
        }
    )
    repo.put_revoked_token("revoked", {"user_id": user["id"], "expires_at": old})
    with repo.database.transaction() as connection:
        connection.execute(
            "INSERT INTO person_identity_links VALUES (?,?,?,?,?)",
            ("cam-001", "a" * 32, "1", "person-old", old),
        )
    for state in ("deleted", "writing"):
        segment, _ = repo.create_segment(
            {
                "camera_id": "cam-001",
                "start_time": old,
                "end_time": format_utc(utc_now() - timedelta(days=39)),
                "relative_path": f"{state}.mp4",
                "format": "mp4",
                "duration_ms": 1000,
                "file_size": 4,
                "source": "central",
                "status": state,
            }
        )
        (settings.storage_root / f"{state}.mp4").write_bytes(b"test")
        with repo.database.transaction() as connection:
            connection.execute(
                "UPDATE recording_segments SET updated_at=? WHERE id=?",
                (old, segment["id"]),
            )
    result = cleanup(repo, settings)
    assert result["history_deleted"]["refresh_tokens"] == 1
    assert result["history_deleted"]["revoked_tokens"] == 1
    assert result["history_deleted"]["person_identity_links"] == 1
    assert result["history_deleted"]["person_track_presence"] == 1
    assert result["history_deleted"]["recording_segments"] == 1
    assert repo.get_event(event["id"]) is None
    assert repo.get_segment_by_path("writing.mp4")["status"] == "writing"
    assert (settings.storage_root / "writing.mp4").exists()


def test_recently_ingested_backfill_survives_history_retention(runtime):
    repo, settings = runtime
    manifest(settings)
    event, path = old_event(repo, settings, "1")
    with repo.database.transaction() as connection:
        connection.execute(
            "UPDATE events SET created_at=? WHERE id=?",
            (format_utc(utc_now()), event["id"]),
        )
    cleanup(repo, settings)
    assert repo.get_event(event["id"]) is not None and path.exists()


def test_protection_cache_observes_replacement_and_expiry(runtime, monkeypatch):
    _, settings = runtime
    manifest(settings, paths=["first.jpg"])
    reader = SnapshotProtectionReader(settings)
    assert reader.read().paths == frozenset({"first.jpg"})
    manifest(settings, paths=["first.jpg", "second.jpg"])
    assert reader.read().paths == frozenset({"first.jpg", "second.jpg"})
    future = utc_now() + timedelta(minutes=3)
    monkeypatch.setattr(
        "server.services.data.app.storage.protection.utc_now", lambda: future
    )
    assert reader.read().reason == "OUTBOX_MANIFEST_STALE"


def queued_push(repo):
    user = repo.create_user(
        {"username": "push-user", "password_hash": "hash", "role": "admin"}
    )
    repo.issue_refresh_token(
        {
            "user_id": user["id"],
            "jti": "push-session",
            "token_hash": "hash",
            "expires_at": format_utc(utc_now() + timedelta(days=90)),
        }
    )
    repo.put_mobile_device(
        {
            "device_id": "b" * 32,
            "user_id": user["id"],
            "refresh_jti": "push-session",
            "token": "token-" + "x" * 30,
            "platform": "android",
            "enabled": True,
            "event_types": None,
        }
    )
    repo.create_event(
        {
            "camera_id": "cam-001",
            "event_type": "person_appeared",
            "occurred_at": format_utc(utc_now()),
        }
    )
    return repo.claim_push()


def test_push_claim_does_not_override_configured_history_retention(runtime):
    repo, settings = runtime
    delivery = queued_push(repo)
    repo.complete_push(delivery["id"], delivery["lease_id"], "sent", None)
    with repo.database.transaction() as connection:
        connection.execute(
            "UPDATE push_deliveries SET updated_at=? WHERE id=?",
            (format_utc(utc_now() - timedelta(days=10)), delivery["id"]),
        )
    assert repo.claim_push() is None
    result = retention_cleanup(
        repo, settings, RetentionRequest(retention_days=30, dry_run=False)
    )
    assert result["history_deleted"]["push_deliveries"] == 0
    assert repo.queue_counts()["push"]["sent"] == 1
    result = cleanup(repo, settings)
    assert result["history_deleted"]["push_deliveries"] == 1


def test_expired_delivery_keeps_inflight_lease_until_lease_expiry(runtime):
    repo, settings = runtime
    delivery = queued_push(repo)
    past = format_utc(utc_now() - timedelta(seconds=1))
    with repo.database.transaction() as connection:
        connection.execute(
            "UPDATE push_deliveries SET expires_at=? WHERE id=?", (past, delivery["id"])
        )
    assert repo.claim_push() is None
    cleanup(repo, settings)
    assert repo.queue_counts()["push"]["sending"] == 1
    with repo.database.transaction() as connection:
        connection.execute(
            "UPDATE push_deliveries SET lease_until=? WHERE id=?",
            (past, delivery["id"]),
        )
    assert repo.claim_push() is None
    assert repo.queue_counts()["push"]["cancelled"] == 1
    assert not repo.complete_push(delivery["id"], delivery["lease_id"], "sent", None)


def test_job_status_tracks_claim_exhaustion_and_unconfigured_requeue(runtime):
    repo, _ = runtime
    event = appearance(repo)
    job = repo.claim_object_job("identity")
    assert repo.get_event(event["id"])["metadata"]["identity"]["status"] == "running"
    with repo.database.transaction() as connection:
        connection.execute(
            "UPDATE object_jobs SET attempts=5,lease_until=? WHERE id=?",
            (format_utc(utc_now() - timedelta(seconds=1)), job["id"]),
        )
    assert repo.claim_object_job("identity") is None
    metadata = repo.get_event(event["id"])["metadata"]["identity"]
    assert (
        metadata["status"] == "failed"
        and metadata["result"]["error_code"] == "ATTEMPTS_EXHAUSTED"
    )
    assert repo.queue_metrics()["identity"]["last_failure_at"] is not None
    assert repo.queue_metrics()["analysis"]["oldest_pending_seconds"] is not None
    analysis = repo.claim_object_job("analysis")
    repo.complete_object_job(
        "analysis",
        analysis["id"],
        ObjectJobCompletion(lease_id=analysis["lease_id"], outcome="unconfigured"),
    )
    assert repo.requeue_unconfigured_objects("analysis") == 1
    assert repo.get_event(event["id"])["metadata"]["analysis"]["status"] == "pending"


@pytest.mark.parametrize("complete_after_disappearance", [False, True])
def test_overlapping_tracks_cannot_share_id_even_when_appearance_times_are_far_apart(
    runtime, complete_after_disappearance
):
    repo, _ = runtime
    stamp = utc_now()
    first = finish(repo, appearance(repo, person="1", at=stamp - timedelta(seconds=60)))
    second_event = appearance(repo, person="2", at=stamp)
    repo.put_live_objects(
        "cam-001",
        LiveObjects(
            tracking_session_id="a" * 32,
            observed_at=stamp,
            frame_width=200,
            frame_height=100,
            objects=[
                {"person_id": "1", "bbox": [0, 0, 50, 100], "confidence": 1},
                {"person_id": "2", "bbox": [100, 0, 150, 100], "confidence": 1},
            ],
        ),
    )
    if complete_after_disappearance:
        for person in ("1", "2"):
            repo.create_event(
                {
                    "camera_id": "cam-001",
                    "person_id": person,
                    "event_type": "person_disappeared",
                    "occurred_at": format_utc(stamp + timedelta(seconds=2)),
                    "metadata": {"tracking_session_id": "a" * 32},
                }
            )
        repo.put_live_objects(
            "cam-001",
            LiveObjects(
                tracking_session_id="a" * 32,
                observed_at=stamp + timedelta(seconds=3),
                frame_width=200,
                frame_height=100,
                objects=[],
            ),
        )
    second = finish(repo, second_event)
    assert second["global_person_id"] != first["global_person_id"]


def test_nonoverlapping_tracks_can_reidentify_after_departure(runtime):
    repo, _ = runtime
    stamp = utc_now() - timedelta(minutes=3)
    first = finish(repo, appearance(repo, person="1", at=stamp))
    repo.create_event(
        {
            "camera_id": "cam-001",
            "person_id": "1",
            "event_type": "person_disappeared",
            "occurred_at": format_utc(stamp + timedelta(seconds=10)),
            "metadata": {"tracking_session_id": "a" * 32},
        }
    )
    second = finish(repo, appearance(repo, person="2", at=stamp + timedelta(minutes=1)))
    assert second["global_person_id"] == first["global_person_id"]


def test_explicit_identity_assignment_cannot_override_concurrent_track_exclusion(
    runtime,
):
    repo, _ = runtime
    first = finish(repo, appearance(repo, person="1"))
    appearance(repo, person="2")
    job = repo.claim_object_job("identity")
    with pytest.raises(ValueError, match="Concurrent camera tracks"):
        repo.complete_object_job(
            "identity",
            job["id"],
            ObjectJobCompletion(
                lease_id=job["lease_id"],
                outcome="complete",
                global_person_id=first["global_person_id"],
            ),
        )


def test_job_max_age_is_validated(runtime):
    _, settings = runtime
    with pytest.raises(ValueError, match="DATA_JOB_MAX_AGE_DAYS"):
        replace(settings, job_max_age_days=0).prepare_directories()
