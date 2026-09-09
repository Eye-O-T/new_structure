"""실제 SQLite·HTTP·spawn으로 리뷰에서 확인한 경쟁·중단 경계를 검증한다."""

from concurrent.futures import ThreadPoolExecutor
import asyncio
from dataclasses import replace
from datetime import timedelta
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import multiprocessing
import os
from pathlib import Path
import shutil
import sqlite3
import threading
import time

from fastapi.testclient import TestClient
import pytest

from ai_cctv_core.time import format_utc, parse_utc, utc_now
from server.services.data.app.config import Settings
from server.services.data.app.database.connection import Database
from server.services.data.app.database.repositories import DataRepository
from server.services.data.app.database.repositories import recordings
from server.services.data.app.main import create_app
from server.services.data.app.storage.recordings import (
    prepare_recording_hook,
    reconcile,
)
from server.services.data.app.workers.recovery import (
    execute_recovery,
    recover_outages,
    RecoveryError,
)
from server.services.data.app.workers.recovery_execution import run_recovery_job
from server.services.data.app.workers import recovery_execution
from server.services.data.app.workers.supervision import WorkerStatus


@pytest.fixture
def runtime(tmp_path):
    settings = Settings(
        database_path=tmp_path / "db/data.db",
        storage_root=tmp_path / "recordings",
        snapshot_root=tmp_path / "snapshots",
        backup_root=tmp_path / "backups",
        internal_token="t" * 32,
        recovery_job_timeout_seconds=2,
        recovery_timeout_seconds=1,
    )
    settings.prepare_directories()
    repo = DataRepository(Database(settings.database_path))
    repo.initialize()
    repo.create_camera(
        {"camera_id": "cam-001", "name": "Camera", "stream_path": "cam-001"}
    )
    return repo, settings


def token(repo, user_id, jti, *, family="family", parent=None):
    return repo.issue_refresh_token(
        {
            "user_id": user_id,
            "jti": jti,
            "family_id": family,
            "token_hash": "hash",
            "expires_at": format_utc(utc_now() + timedelta(days=1)),
            "rotated_from_jti": parent,
        }
    )


def test_logout_revokes_rotated_family_but_keeps_other_user_and_login(runtime):
    repo, settings = runtime
    first = repo.create_user(
        {"username": "first", "password_hash": "hash", "role": "admin"}
    )["id"]
    second = repo.create_user(
        {"username": "second", "password_hash": "hash", "role": "viewer"}
    )["id"]
    token(repo, first, "old")
    token(repo, first, "rotated", parent="old")
    token(repo, first, "other-login", family="other")
    token(repo, second, "other-user")
    client = TestClient(create_app(settings=settings, repository=repo))
    headers = {"X-Internal-Token": settings.internal_token}
    assert (
        client.delete("/internal/v1/tokens/refresh/old", headers=headers).status_code
        == 204
    )
    assert (
        client.delete("/internal/v1/tokens/refresh/old", headers=headers).status_code
        == 204
    )
    assert not client.get(
        "/internal/v1/tokens/families/family",
        params={"user_id": first},
        headers=headers,
    ).json()["active"]
    assert repo.get_refresh_token("rotated")["revoked_at"] is not None
    with pytest.raises(PermissionError):
        token(repo, first, "after-logout", parent="rotated")
    with pytest.raises(PermissionError):
        token(repo, first, "reused-family")
    assert repo.token_family_is_active(first, "other")
    assert repo.token_family_is_active(second, "family")
    assert (
        client.delete(
            "/internal/v1/tokens/families/other",
            params={"user_id": first},
            headers=headers,
        ).status_code
        == 204
    )
    assert not repo.token_family_is_active(first, "other")


def test_refresh_and_family_logout_are_atomic_under_concurrency(runtime):
    repo, _ = runtime
    user = repo.create_user(
        {"username": "viewer", "password_hash": "hash", "role": "viewer"}
    )["id"]
    for attempt in range(8):
        family = f"family-{attempt}"
        old, new = f"old-{attempt}", f"new-{attempt}"
        token(repo, user, old, family=family)
        barrier = threading.Barrier(2)

        def rotate():
            barrier.wait()
            try:
                token(repo, user, new, family=family, parent=old)
            except PermissionError:
                pass

        def logout():
            barrier.wait()
            assert repo.delete_refresh_token(old)

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(rotate), executor.submit(logout)]
            for future in futures:
                future.result(timeout=5)
        assert not repo.token_family_is_active(user, family)
        latest = repo.get_refresh_token(new)
        assert latest is None or latest["revoked_at"] is not None


def test_refresh_cannot_change_family_and_legacy_null_family_is_revoked(runtime):
    repo, _ = runtime
    user = repo.create_user(
        {"username": "viewer", "password_hash": "hash", "role": "viewer"}
    )["id"]
    token(repo, user, "legacy", family="legacy")
    with repo.database.transaction() as connection:
        connection.execute(
            "UPDATE refresh_tokens SET family_id=NULL WHERE jti='legacy'"
        )
    with pytest.raises(PermissionError, match="family"):
        token(repo, user, "escape", family="different", parent="legacy")
    token(repo, user, "new", family="legacy", parent="legacy")
    assert repo.delete_refresh_token("legacy")
    assert not repo.token_family_is_active(user, "legacy")


@pytest.fixture
def http_services():
    state = {"mode": "empty", "paths": [], "file_started": threading.Event()}
    content = b"review-recording" * 128
    relative = "2026/09/09/20260909T010000.000000Z_000000.ts"

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?")[0]
            state["paths"].append(path)
            if path.endswith("/manifest"):
                body = {"camera_id": "cam-001", "items": []}
                if state["mode"] in {"file", "slow"}:
                    body["items"] = [
                        {
                            "camera_id": "cam-001",
                            "start_time": "2026-09-09T01:00:00Z",
                            "end_time": "2026-09-09T01:00:10Z",
                            "relative_path": relative,
                            "size": len(content),
                            "sha256": hashlib.sha256(content).hexdigest(),
                        }
                    ]
                raw = (
                    json.dumps(body).encode()
                    if state["mode"] != "invalid"
                    else b"invalid-json"
                )
            else:
                raw = content
            self.send_response(200)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            try:
                if "/files/" in path:
                    state["file_started"].set()
                if state["mode"] == "slow" and "/files/" in path:
                    for byte in raw:
                        self.wfile.write(bytes([byte]))
                        self.wfile.flush()
                        time.sleep(0.03)
                else:
                    self.wfile.write(raw)
            except (ConnectionError, OSError):
                pass

        def do_POST(self):
            self.rfile.read(int(self.headers["Content-Length"]))
            raw = b'{"idempotent_replay":false}'
            self.send_response(201)
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.01), daemon=True
    )
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def connect_edge(repo, base, device="old"):
    repo.update_camera(
        "cam-001",
        {
            "edge_device_id": device,
            "edge_management_url": f"{base}/{device}",
            "edge_recovery_url": f"{base}/{device}",
            "edge_auth_token": "e" * 32,
        },
    )


def outage(repo, start="2026-09-09T01:00:00.000Z", end="2026-09-09T01:01:00.000Z"):
    repo.note_recovery_event(
        camera_id="cam-001",
        event_type="central_connection_lost",
        occurred_at=start,
        max_attempts=3,
        settle_seconds=0,
    )
    return repo.note_recovery_event(
        camera_id="cam-001",
        event_type="central_connection_restored",
        occurred_at=end,
        max_attempts=3,
        settle_seconds=0,
    )


def test_recovery_keeps_original_edge_after_camera_replacement(runtime, http_services):
    repo, settings = runtime
    base, state = http_services
    connect_edge(repo, base)
    job = outage(repo)
    connect_edge(repo, base, "new")
    assert repo.get_edge_device("old") is not None
    claimed = repo.claim_due_recovery_job()
    assert claimed["edge_device_id"] == "old"
    execute_recovery(claimed, repo, replace(settings, recovery_job_timeout_seconds=10))
    assert state["paths"] == ["/old/v1/recovery/manifest"]
    assert repo.get_recovery_job(job["id"])["status"] == "completed"
    newer = outage(repo, "2026-09-09T02:00:00.000Z", "2026-09-09T02:01:00.000Z")
    assert newer["edge_device_id"] == "new"


def test_event_origin_survives_replacement_between_event_and_job_commit(
    runtime, http_services
):
    repo, _ = runtime
    base, _ = http_services
    connect_edge(repo, base)
    event = repo.create_event(
        {
            "camera_id": "cam-001",
            "event_type": "central_connection_lost",
            "occurred_at": "2026-09-09T01:00:00.000Z",
        }
    )
    connect_edge(repo, base, "new")
    assert repo.get_edge_device("old") is not None
    job = repo.note_recovery_event(
        camera_id="cam-001",
        event_type=event["event_type"],
        occurred_at=event["occurred_at"],
        max_attempts=3,
        origin_recorded=True,
        origin_edge_device_id=event["metadata"]["recovery_edge_device_id"],
    )
    assert job["edge_device_id"] == "old"
    assert (
        repo.note_recovery_event(
            camera_id="cam-001",
            event_type="central_connection_restored",
            occurred_at="2026-09-09T01:01:00.000Z",
            max_attempts=3,
        )
        is None
    )
    assert repo.get_recovery_job(job["id"])["outage_ended_at"] is None


def orphan_recording(repo, settings):
    event = repo.create_event(
        {
            "camera_id": "cam-001",
            "event_type": "person_detected",
            "occurred_at": "2026-08-22T12:35:00.000Z",
        }
    )
    path = settings.storage_root / "cam-001/2026/08/22/20260822T123456-123456Z.mp4"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"complete-recording")
    stamp = parse_utc("2026-08-22T12:35:56.000Z").timestamp()
    os.utime(path, (stamp, stamp))
    return event, path


def test_reconcile_rolls_back_segment_when_event_linking_fails(runtime, monkeypatch):
    repo, settings = runtime
    event, path = orphan_recording(repo, settings)
    original = recordings._link_segment_to_events

    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise sqlite3.OperationalError("injected link commit interruption")

    monkeypatch.setattr(recordings, "_link_segment_to_events", interrupted)
    with pytest.raises(sqlite3.OperationalError):
        reconcile(repo, settings)
    assert (
        repo.get_segment_by_path(path.relative_to(settings.storage_root).as_posix())
        is None
    )
    assert repo.get_event(event["id"])["recording_segment_ids"] == []
    monkeypatch.setattr(recordings, "_link_segment_to_events", original)
    reconcile(repo, settings)
    assert len(repo.get_event(event["id"])["recording_segment_ids"]) == 1


def test_reconcile_repairs_legacy_committed_segment_without_event_link(runtime):
    repo, settings = runtime
    event, path = orphan_recording(repo, settings)
    repo.create_segment(
        prepare_recording_hook(
            camera_id="cam-001",
            segment_path=str(path),
            duration_seconds=60,
            settings=settings,
        )
    )
    result = reconcile(repo, settings)
    assert result["repaired_event_links"] == 1
    assert len(repo.get_event(event["id"])["recording_segment_ids"]) == 1
    assert reconcile(repo, settings)["repaired_event_links"] == 0


@pytest.mark.parametrize("mode", ["file", "invalid", "slow"])
def test_recovery_child_completion_error_and_deadline_cleanup(
    runtime, http_services, mode
):
    repo, settings = runtime
    base, state = http_services
    state["mode"] = mode
    connect_edge(repo, base)
    job = outage(repo)
    next_job = outage(repo, "2026-09-09T02:00:00.000Z", "2026-09-09T02:01:00.000Z")
    settings = replace(settings, recovery_data_base_url=base + "/internal/v1")
    before = {process.pid for process in multiprocessing.active_children()}
    started = time.monotonic()
    execute_recovery(repo.claim_due_recovery_job(), repo, settings)
    elapsed = time.monotonic() - started
    assert {process.pid for process in multiprocessing.active_children()} == before
    assert list(settings.storage_root.rglob("*.part")) == []
    stored = repo.get_recovery_job(job["id"])
    assert stored["status"] == ("completed" if mode == "file" else "failed")
    if mode == "slow":
        assert state["file_started"].is_set()
        assert stored["last_error"] == "RECOVERY_JOB_TIMEOUT"
        assert elapsed < settings.recovery_job_timeout_seconds + 3
    if mode != "file":
        assert stored["next_retry_at"] is not None
    state["mode"] = "empty"
    claimed = repo.claim_due_recovery_job()
    assert claimed["id"] == next_job["id"]
    execute_recovery(claimed, repo, settings)
    assert repo.get_recovery_job(next_job["id"])["status"] == "completed"


def test_recovery_stop_terminates_inflight_child_and_removes_partial_file(
    runtime, http_services
):
    repo, settings = runtime
    base, state = http_services
    state["mode"] = "slow"
    connect_edge(repo, base)
    outage(repo)
    job = repo.claim_due_recovery_job()
    stop = threading.Event()
    before = {process.pid for process in multiprocessing.active_children()}
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            run_recovery_job,
            job,
            replace(settings, recovery_job_timeout_seconds=10),
            lambda _stage: None,
            stop_event=stop,
        )
        assert state["file_started"].wait(5)
        stop.set()
        with pytest.raises(RecoveryError, match="RECOVERY_INTERRUPTED"):
            future.result(timeout=3)
    assert {process.pid for process in multiprocessing.active_children()} == before
    assert list(settings.storage_root.rglob("*.part")) == []


def test_unexpected_child_exception_is_reported_without_live_process(
    runtime, http_services
):
    repo, settings = runtime
    base, _ = http_services
    connect_edge(repo, base)
    job = outage(repo)
    claimed = repo.claim_due_recovery_job()
    claimed["outage_started_at"] = None
    before = {process.pid for process in multiprocessing.active_children()}
    execute_recovery(claimed, repo, settings)
    assert repo.get_recovery_job(job["id"])["last_error"] == "RECOVERY_WORKER_ERROR"
    assert {process.pid for process in multiprocessing.active_children()} == before


@pytest.mark.asyncio
async def test_worker_shutdown_persists_interruption_and_waits_for_child_cleanup(
    runtime, http_services
):
    repo, settings = runtime
    base, state = http_services
    state["mode"] = "slow"
    connect_edge(repo, base)
    job = outage(repo)
    settings = replace(
        settings, recovery_poll_interval_seconds=0.01, recovery_job_timeout_seconds=10
    )
    before = {process.pid for process in multiprocessing.active_children()}
    task = asyncio.create_task(recover_outages(repo, settings))
    try:
        assert await asyncio.to_thread(state["file_started"].wait, 5)
    finally:
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
    assert repo.get_recovery_job(job["id"])["last_error"] == "RECOVERY_INTERRUPTED"
    assert {process.pid for process in multiprocessing.active_children()} == before
    assert list(settings.storage_root.rglob("*.part")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("termination_raises", [False, True])
async def test_unstoppable_child_blocks_further_claims_and_keeps_worker_degraded(
    runtime, monkeypatch, termination_raises
):
    repo, settings = runtime
    connect_edge(repo, "http://unused")
    job = outage(repo)
    outage(repo, "2026-09-09T02:00:00.000Z", "2026-09-09T02:01:00.000Z")
    calls = []

    class FakePipe:
        def poll(self, _timeout=0):
            return True

        def recv(self):
            return {"kind": "complete", "summary": {}}

        def close(self):
            calls.append("pipe_close")

    class UnstoppableProcess:
        pid = 123

        def start(self):
            calls.append("start")

        def join(self, timeout):
            pass

        def is_alive(self):
            return True

        def terminate(self):
            calls.append("terminate")
            if termination_raises:
                raise PermissionError("termination denied")

        def kill(self):
            calls.append("kill")
            if termination_raises:
                raise PermissionError("kill denied")

        def close(self):
            pytest.fail("must not close a still-running process")

    class FakeContext:
        def Pipe(self, *, duplex):
            return FakePipe(), FakePipe()

        def Process(self, **_kwargs):
            return UnstoppableProcess()

    monkeypatch.setattr(
        recovery_execution.multiprocessing, "get_context", lambda _: FakeContext()
    )
    original_claim = repo.claim_due_recovery_job

    def claim():
        calls.append("claim")
        return original_claim()

    monkeypatch.setattr(repo, "claim_due_recovery_job", claim)
    status = WorkerStatus(alive=True)
    task = asyncio.create_task(
        recover_outages(
            repo, replace(settings, recovery_poll_interval_seconds=0.01), status
        )
    )
    try:

        async def wait_for_fatal():
            while status.last_error != "RECOVERY_PROCESS_DID_NOT_STOP":
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_for_fatal(), 3)
        await asyncio.sleep(0.05)
        assert calls.count("claim") == 1
        assert "terminate" in calls and "kill" in calls
        assert calls.count("pipe_close") == 3
        assert status.snapshot()["status"] == "error"
        app = create_app(settings=settings, repository=repo)
        app.state.workers = {"recovery": status}
        response = TestClient(app).get("/health/ready")
        assert response.status_code == 503
        assert (
            response.json()["workers"]["recovery"]["last_error"]
            == "RECOVERY_PROCESS_DID_NOT_STOP"
        )
        assert not task.done()
        assert repo.get_recovery_job(job["id"])["status"] == "downloading"
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_migration_binds_existing_recovery_to_retained_device(tmp_path):
    migrations = Path(recordings.__file__).parents[1] / "migrations"
    old_migrations = tmp_path / "old-migrations"
    old_migrations.mkdir()
    for migration in sorted(migrations.glob("*.sql")):
        if migration.name < "009":
            shutil.copyfile(migration, old_migrations / migration.name)
    path = tmp_path / "old.db"
    old = Database(path, migrations_dir=old_migrations)
    old.initialize()
    with old.transaction() as connection:
        connection.execute(
            "INSERT INTO cameras(camera_id,name,stream_path,edge_device_id,created_at,updated_at) VALUES ('cam-001','Camera','cam-001','old','2026-01-01','2026-01-01')"
        )
        connection.execute(
            "INSERT INTO edge_devices VALUES ('old','http://old','http://old','token','2026-01-01','2026-01-01')"
        )
        connection.execute(
            "INSERT INTO recovery_jobs(camera_id,outage_started_at,outage_ended_at,status,created_at,updated_at) VALUES ('cam-001','2026-01-01','2026-01-02','waiting_for_recovery','2026-01-01','2026-01-01')"
        )
    upgraded = Database(path)
    upgraded.initialize()
    upgraded.initialize()
    with upgraded.connection() as connection:
        assert (
            connection.execute("SELECT edge_device_id FROM recovery_jobs").fetchone()[0]
            == "old"
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("DELETE FROM edge_devices WHERE edge_device_id='old'")
