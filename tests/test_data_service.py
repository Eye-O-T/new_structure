from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.services.data.app.config import Settings
from server.services.data.app.main import create_app


TOKEN = "test-internal-token"
HEADERS = {"X-Internal-Token": TOKEN}
BASE = "/internal/v1"
SCOPED_TOKENS = {
    "external": "e" * 40,
    "inference": "i" * 40,
    "media": "m" * 40,
    "recovery": "r" * 40,
}


@pytest.fixture
def data_client(tmp_path: Path):
    settings = Settings(
        database_path=tmp_path / "database" / "ai_cctv.db",
        storage_root=tmp_path / "recordings",
        snapshot_root=tmp_path / "snapshots",
        backup_root=tmp_path / "backups",
        internal_token=TOKEN,
        busy_timeout_ms=750,
    )
    app = create_app(settings=settings)
    with TestClient(app) as client:
        yield client, settings


@pytest.fixture
def scoped_data_client(tmp_path: Path):
    settings = Settings(
        database_path=tmp_path / "database" / "ai_cctv.db",
        storage_root=tmp_path / "recordings",
        snapshot_root=tmp_path / "snapshots",
        backup_root=tmp_path / "backups",
        internal_token="",
        data_external_token=SCOPED_TOKENS["external"],
        data_inference_token=SCOPED_TOKENS["inference"],
        data_media_token=SCOPED_TOKENS["media"],
        data_recovery_token=SCOPED_TOKENS["recovery"],
        busy_timeout_ms=750,
    )
    app = create_app(settings=settings)
    with TestClient(app) as client:
        yield client


def test_scoped_internal_tokens_enforce_least_privilege(scoped_data_client):
    client = scoped_data_client
    headers = {
        scope: {"X-Internal-Token": token}
        for scope, token in SCOPED_TOKENS.items()
    }

    assert client.get(f"{BASE}/users", headers=headers["external"]).status_code == 200
    assert client.get(f"{BASE}/users", headers=headers["inference"]).status_code == 403
    assert (
        client.get(f"{BASE}/cameras/enabled", headers=headers["inference"]).status_code
        == 200
    )
    assert (
        client.get(f"{BASE}/cameras/enabled", headers=headers["external"]).status_code
        == 403
    )
    assert (
        client.patch(
            f"{BASE}/cameras/missing/status",
            headers=headers["inference"],
            json={"status": "online"},
        ).status_code
        == 404
    )
    assert (
        client.patch(
            f"{BASE}/cameras/missing/status",
            headers=headers["external"],
            json={"status": "online"},
        ).status_code
        == 403
    )
    assert (
        client.post(f"{BASE}/events", headers=headers["inference"], json={}).status_code
        == 422
    )
    assert (
        client.post(f"{BASE}/events", headers=headers["media"], json={}).status_code
        == 403
    )
    assert (
        client.post(
            f"{BASE}/hooks/recording-complete", headers=headers["media"], json={}
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"{BASE}/hooks/recording-complete",
            headers=headers["external"],
            json={},
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"{BASE}/recording-segments", headers=headers["recovery"], json={}
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"{BASE}/recording-segments", headers=headers["media"], json={}
        ).status_code
        == 403
    )
    assert (
        client.get(
            f"{BASE}/users", headers={"X-Internal-Token": "unknown-token"}
        ).status_code
        == 401
    )


def test_partial_scoped_tokens_cannot_fall_back_to_legacy(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "database" / "ai_cctv.db",
        storage_root=tmp_path / "recordings",
        snapshot_root=tmp_path / "snapshots",
        backup_root=tmp_path / "backups",
        internal_token=TOKEN,
        data_external_token=SCOPED_TOKENS["external"],
    )
    assert settings.data_api_tokens()["inference"] == ""
    with pytest.raises(ValueError, match="must be configured together"):
        settings.prepare_directories()


@pytest.mark.parametrize("seconds", (9, 301))
def test_data_settings_reject_segment_duration_outside_srs_range(
    tmp_path: Path, seconds: int
) -> None:
    settings = Settings(
        database_path=tmp_path / "database" / "ai_cctv.db",
        storage_root=tmp_path / "recordings",
        snapshot_root=tmp_path / "snapshots",
        backup_root=tmp_path / "backups",
        internal_token=TOKEN,
        central_recording_segment_seconds=seconds,
    )

    with pytest.raises(ValueError, match="range 10..300"):
        settings.prepare_directories()


def test_data_settings_preserve_legacy_runtime_token_fallback(monkeypatch) -> None:
    for name in (
        "DATA_EXTERNAL_TOKEN",
        "DATA_INFERENCE_TOKEN",
        "DATA_MEDIA_TOKEN",
        "DATA_RECOVERY_TOKEN",
        "DATA_INTERNAL_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", TOKEN)
    settings = Settings.from_env()
    assert set(settings.data_api_tokens().values()) == {TOKEN}


def _create_user(client: TestClient, username: str, role: str = "viewer") -> dict:
    response = client.post(
        f"{BASE}/users",
        headers=HEADERS,
        json={
            "username": username,
            "password_hash": f"hash-for-{username}",
            "role": role,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_camera(client: TestClient, camera_id: str) -> dict:
    response = client.post(
        f"{BASE}/cameras",
        headers=HEADERS,
        json={
            "camera_id": camera_id,
            "name": camera_id,
            "stream_path": camera_id,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def _create_segment(
    client: TestClient,
    settings: Settings,
    *,
    camera_id: str = "cam-001",
    relative_path: str,
    start: datetime,
    end: datetime,
    idempotency_key: str | None = None,
) -> dict:
    target = settings.storage_root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"video-segment")
    response = client.post(
        f"{BASE}/recording-segments",
        headers=HEADERS,
        json={
            "camera_id": camera_id,
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "relative_path": relative_path,
            "format": "mp4",
            "source": "central",
            "idempotency_key": idempotency_key,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_schema_indexes_pragmas_and_foreign_keys(data_client) -> None:
    client, settings = data_client
    assert client.get("/health/live").status_code == 200
    ready = client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json()["database"]["journal_mode"] == "wal"
    assert ready.json()["database"]["foreign_keys"] is True
    assert ready.json()["storage"]["free_bytes"] > 0

    with sqlite3.connect(settings.database_path) as connection:
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert "idx_segments_camera_time" in indexes
        assert "idx_events_camera_time_type" in indexes
        assert "idx_event_segments_segment" in indexes

    _create_camera(client, "cam-001")
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO recording_segments(
                    camera_id, start_time, end_time, relative_path, format, codec,
                    duration_ms, file_size, source, status, created_at, updated_at
                ) VALUES ('missing-camera', '2026-01-01T00:00:00.000Z',
                    '2026-01-01T00:01:00.000Z', 'x.mp4', 'mp4', 'h264',
                    60000, 1, 'central', 'ready',
                    '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z')
                """
            )


def test_internal_routes_require_token_and_use_json_error_shape(data_client) -> None:
    client, _settings = data_client
    response = client.get(f"{BASE}/cameras")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_INTERNAL_TOKEN"


def test_overlap_boundaries_and_pagination(data_client) -> None:
    client, settings = data_client
    _create_camera(client, "cam-001")
    base = datetime(2026, 8, 22, tzinfo=UTC)
    first = _create_segment(
        client,
        settings,
        relative_path="cam-001/first.mp4",
        start=base,
        end=base + timedelta(seconds=60),
    )
    second = _create_segment(
        client,
        settings,
        relative_path="cam-001/second.mp4",
        start=base + timedelta(seconds=60),
        end=base + timedelta(seconds=120),
    )

    response = client.get(
        f"{BASE}/recording-segments/search",
        headers=HEADERS,
        params={
            "camera_id": "cam-001",
            "from": (base + timedelta(seconds=60)).isoformat(),
            "to": (base + timedelta(seconds=61)).isoformat(),
            "limit": 1,
            "offset": 0,
        },
    )
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == [second["id"]]
    assert first["id"] != second["id"]
    assert response.json()["limit"] == 1


def test_segment_idempotency_and_file_stat(data_client) -> None:
    client, settings = data_client
    _create_camera(client, "cam-001")
    start = datetime(2026, 8, 22, tzinfo=UTC)
    first = _create_segment(
        client,
        settings,
        relative_path="cam-001/idempotent.mp4",
        start=start,
        end=start + timedelta(seconds=60),
        idempotency_key="same-hook",
    )
    second = _create_segment(
        client,
        settings,
        relative_path="cam-001/idempotent.mp4",
        start=start,
        end=start + timedelta(seconds=60),
        idempotency_key="same-hook",
    )
    assert first["id"] == second["id"]
    assert first["file_size"] == len(b"video-segment")
    assert second["idempotent_replay"] is True


def test_recording_complete_hook_derives_metadata_and_is_idempotent(
    data_client,
) -> None:
    client, settings = data_client
    _create_camera(client, "cam-001")
    target = settings.storage_root / "cam-001/hook.mp4"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"hook-video")
    expected_end = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    os.utime(target, (expected_end.timestamp(), expected_end.timestamp()))
    form = {
        "camera_id": "cam-001",
        "segment_path": str(target),
        "duration_seconds": "10",
    }
    first = client.post(f"{BASE}/hooks/recording-complete", headers=HEADERS, data=form)
    second = client.post(f"{BASE}/hooks/recording-complete", headers=HEADERS, data=form)
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["relative_path"] == "cam-001/hook.mp4"
    assert first.json()["file_size"] == len(b"hook-video")
    assert first.json()["duration_ms"] == 10_000
    assert first.json()["end_time"] == "2026-08-22T12:00:00.000Z"
    assert first.json()["start_time"] == "2026-08-22T11:59:50.000Z"
    assert second.json()["idempotent_replay"] is True


def test_event_has_primary_and_many_to_many_segment_links(data_client) -> None:
    client, settings = data_client
    _create_camera(client, "cam-001")
    base = datetime(2026, 8, 22, tzinfo=UTC)
    first = _create_segment(
        client,
        settings,
        relative_path="cam-001/a.mp4",
        start=base,
        end=base + timedelta(seconds=60),
    )
    second = _create_segment(
        client,
        settings,
        relative_path="cam-001/b.mp4",
        start=base + timedelta(seconds=60),
        end=base + timedelta(seconds=120),
    )
    response = client.post(
        f"{BASE}/events",
        headers=HEADERS,
        json={
            "camera_id": "cam-001",
            "event_type": "person_detected",
            "occurred_at": (base + timedelta(seconds=60)).isoformat(),
            "track_id": "track-7",
            "confidence": 0.91,
            "recording_segment_ids": [first["id"]],
            "metadata": {"label": "사람"},
        },
    )
    assert response.status_code == 201, response.text
    event = response.json()
    assert event["recording_segment_ids"] == [first["id"], second["id"]]
    assert event["recording_segment_id"] == first["id"]
    assert event["metadata"] == {"label": "사람"}


def test_later_segment_is_linked_to_event_pre_and_post_roll_window(data_client) -> None:
    client, settings = data_client
    _create_camera(client, "cam-001")
    occurred = datetime(2026, 8, 22, 8, 1, tzinfo=UTC)
    created = client.post(
        f"{BASE}/events",
        headers=HEADERS,
        json={
            "camera_id": "cam-001",
            "event_type": "person_appeared",
            "occurred_at": occurred.isoformat(),
        },
    ).json()
    assert created["recording_segment_ids"] == []

    segment = _create_segment(
        client,
        settings,
        relative_path="cam-001/future-index.mp4",
        start=occurred + timedelta(seconds=5),
        end=occurred + timedelta(seconds=15),
    )
    stored = client.get(f"{BASE}/events/{created['id']}", headers=HEADERS).json()
    assert stored["recording_segment_ids"] == [segment["id"]]
    assert stored["recording_segment_id"] == segment["id"]


def test_camera_acl_filters_viewer_but_not_admin(data_client) -> None:
    client, _settings = data_client
    viewer = _create_user(client, "viewer")
    admin = _create_user(client, "admin", role="admin")
    _create_camera(client, "cam-001")
    _create_camera(client, "cam-002")
    grant = client.put(
        f"{BASE}/users/{viewer['id']}/camera-permissions/cam-002",
        headers=HEADERS,
    )
    assert grant.status_code == 200

    viewer_result = client.get(
        f"{BASE}/cameras", headers=HEADERS, params={"user_id": viewer["id"]}
    ).json()
    admin_result = client.get(
        f"{BASE}/cameras", headers=HEADERS, params={"user_id": admin["id"]}
    ).json()
    assert [item["camera_id"] for item in viewer_result["items"]] == ["cam-002"]
    assert {item["camera_id"] for item in admin_result["items"]} == {
        "cam-001",
        "cam-002",
    }


def test_camera_publish_credential_is_internal_and_cascades(data_client) -> None:
    client, _settings = data_client
    _create_camera(client, "cam-001")
    stored = client.put(
        f"{BASE}/cameras/cam-001/publish-credential",
        headers=HEADERS,
        json={"username": "cam-001", "password_hash": "$argon2id$dynamic-test"},
    )
    assert stored.status_code == 200
    assert stored.json()["password_hash"].startswith("$argon2")
    assert (
        client.get(
            f"{BASE}/cameras/cam-001/publish-credential", headers=HEADERS
        ).status_code
        == 200
    )
    assert client.delete(f"{BASE}/cameras/cam-001", headers=HEADERS).status_code == 204
    assert (
        client.get(
            f"{BASE}/cameras/cam-001/publish-credential", headers=HEADERS
        ).status_code
        == 404
    )


@pytest.mark.parametrize(
    "bad_path", ["../escape.mp4", "/absolute.mp4", "C:\\escape.mp4"]
)
def test_path_traversal_and_absolute_paths_are_rejected(
    data_client, bad_path: str
) -> None:
    client, _settings = data_client
    _create_camera(client, "cam-001")
    start = datetime(2026, 8, 22, tzinfo=UTC)
    response = client.post(
        f"{BASE}/recording-segments",
        headers=HEADERS,
        json={
            "camera_id": "cam-001",
            "start_time": start.isoformat(),
            "end_time": (start + timedelta(seconds=60)).isoformat(),
            "relative_path": bad_path,
            "format": "mp4",
            "source": "central",
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_STORAGE_PATH"


def test_reconcile_marks_missing_and_reports_orphan(data_client) -> None:
    client, settings = data_client
    _create_camera(client, "cam-001")
    start = datetime(2026, 8, 22, tzinfo=UTC)
    segment = _create_segment(
        client,
        settings,
        relative_path="cam-001/missing.mp4",
        start=start,
        end=start + timedelta(seconds=60),
    )
    (settings.storage_root / segment["relative_path"]).unlink()
    orphan = settings.storage_root / "cam-001/orphan.mp4"
    orphan.write_bytes(b"orphan")

    response = client.post(f"{BASE}/reconcile", headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["missing"] == ["cam-001/missing.mp4"]
    assert response.json()["orphaned"] == ["cam-001/orphan.mp4"]
    stored = client.get(
        f"{BASE}/recording-segments/{segment['id']}", headers=HEADERS
    ).json()
    assert stored["status"] == "missing"


def test_reconcile_indexes_completed_mediamtx_segment_after_hook_failure(
    data_client,
) -> None:
    client, settings = data_client
    _create_camera(client, "cam-001")
    start = datetime(2026, 8, 22, 12, 34, 56, 123456, tzinfo=UTC)
    end = start + timedelta(seconds=60)
    relative_path = "cam-001/2026/08/22/20260822T123456-123456Z.mp4"
    target = settings.storage_root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"completed-central-recording")
    timestamp = end.timestamp()
    os.utime(target, (timestamp, timestamp))

    response = client.post(f"{BASE}/reconcile", headers=HEADERS)

    assert response.status_code == 200, response.text
    assert response.json()["indexed_orphans"] == [relative_path]
    assert response.json()["orphaned"] == []
    indexed = client.get(
        f"{BASE}/recording-segments/search",
        headers=HEADERS,
        params={
            "camera_id": "cam-001",
            "from": start.isoformat(),
            "to": (end + timedelta(seconds=1)).isoformat(),
        },
    )
    assert indexed.status_code == 200, indexed.text
    assert indexed.json()["items"][0]["relative_path"] == relative_path
    assert indexed.json()["items"][0]["source"] == "central"

    replay = client.post(f"{BASE}/reconcile", headers=HEADERS)
    assert replay.status_code == 200
    assert replay.json()["indexed_orphans"] == []


def test_backup_and_retention_cleanup(data_client) -> None:
    client, settings = data_client
    _create_camera(client, "cam-001")
    start = datetime(2025, 1, 1, tzinfo=UTC)
    segment = _create_segment(
        client,
        settings,
        relative_path="cam-001/old.mp4",
        start=start,
        end=start + timedelta(seconds=60),
    )
    dry_run = client.post(
        f"{BASE}/retention/cleanup",
        headers=HEADERS,
        json={"before": "2026-01-01T00:00:00Z", "dry_run": True},
    )
    assert dry_run.status_code == 200
    assert dry_run.json()["segment_ids"] == [segment["id"]]
    cleanup = client.post(
        f"{BASE}/retention/cleanup",
        headers=HEADERS,
        json={"before": "2026-01-01T00:00:00Z", "dry_run": False},
    )
    assert cleanup.json()["deleted"] == 1
    assert not (settings.storage_root / "cam-001/old.mp4").exists()

    backup = client.post(
        f"{BASE}/backup", headers=HEADERS, json={"filename": "manual.db"}
    )
    assert backup.status_code == 201, backup.text
    backup_path = settings.backup_root / backup.json()["relative_path"]
    assert backup_path.is_file()
    with sqlite3.connect(backup_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("file_exists", [True, False])
def test_reconcile_completes_interrupted_retention_delete(
    data_client, file_exists: bool
) -> None:
    client, settings = data_client
    _create_camera(client, "cam-001")
    start = datetime(2025, 1, 1, tzinfo=UTC)
    segment = _create_segment(
        client,
        settings,
        relative_path="cam-001/interrupted.mp4",
        start=start,
        end=start + timedelta(seconds=60),
    )
    target = settings.storage_root / segment["relative_path"]
    if not file_exists:
        target.unlink()
    repository = client.app.state.repository
    repository.set_segment_status(segment["id"], "deleting")

    response = client.post(f"{BASE}/reconcile", headers=HEADERS)

    assert response.status_code == 200
    assert response.json()["completed_deletions"] == [
        "cam-001/interrupted.mp4"
    ]
    assert not target.exists()
    stored = client.get(
        f"{BASE}/recording-segments/{segment['id']}", headers=HEADERS
    ).json()
    assert stored["status"] == "deleted"


def test_refresh_rotation_and_revoked_token_state(data_client) -> None:
    client, _settings = data_client
    user = _create_user(client, "token-user")
    expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    old = client.post(
        f"{BASE}/tokens/refresh",
        headers=HEADERS,
        json={
            "user_id": user["id"],
            "jti": "old-jti",
            "token_hash": "old-hash",
            "expires_at": expires,
            "family_id": "family-1",
        },
    )
    assert old.status_code == 201, old.text
    new = client.post(
        f"{BASE}/tokens/refresh",
        headers=HEADERS,
        json={
            "user_id": user["id"],
            "jti": "new-jti",
            "token_hash": "new-hash",
            "expires_at": expires,
            "family_id": "family-1",
            "rotated_from_jti": "old-jti",
        },
    )
    assert new.status_code == 201, new.text
    old_state = client.get(f"{BASE}/tokens/refresh/old-jti", headers=HEADERS).json()
    assert old_state["revoked_at"] is not None
    assert old_state["replaced_by_jti"] == "new-jti"

    put = client.put(
        f"{BASE}/tokens/revoked/access-jti",
        headers=HEADERS,
        json={
            "user_id": user["id"],
            "expires_at": expires,
            "reason": "logout",
        },
    )
    assert put.status_code == 200
    assert (
        client.get(f"{BASE}/tokens/revoked/access-jti", headers=HEADERS).json()[
            "reason"
        ]
        == "logout"
    )
    assert (
        client.delete(f"{BASE}/tokens/refresh/new-jti", headers=HEADERS).status_code
        == 204
    )
    missing = client.get(f"{BASE}/tokens/refresh/new-jti", headers=HEADERS)
    assert missing.status_code == 404


def test_naive_timestamps_are_rejected(data_client) -> None:
    client, _settings = data_client
    _create_camera(client, "cam-001")
    response = client.post(
        f"{BASE}/events",
        headers=HEADERS,
        json={
            "camera_id": "cam-001",
            "event_type": "person_detected",
            "occurred_at": "2026-08-22T10:00:00",
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_first_start_bootstraps_admin_and_cameras_idempotently(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """schema_version: 1
cameras:
  - camera_id: cam-001
    name: Entrance
""",
        encoding="utf-8",
    )
    settings = Settings(
        database_path=tmp_path / "database" / "ai_cctv.db",
        storage_root=tmp_path / "recordings",
        snapshot_root=tmp_path / "snapshots",
        backup_root=tmp_path / "backups",
        internal_token=TOKEN,
        initial_admin_username="admin",
        initial_admin_password_hash="$argon2id$bootstrap-test",
        config_path=config_path,
    )
    for _ in range(2):
        with TestClient(create_app(settings=settings)) as client:
            users = client.get(f"{BASE}/users", headers=HEADERS).json()["items"]
            cameras = client.get(f"{BASE}/cameras", headers=HEADERS).json()["items"]
            assert [user["username"] for user in users] == ["admin"]
            assert [camera["camera_id"] for camera in cameras] == ["cam-001"]


def test_edge_metadata_profiles_and_runtime_state_are_separate(data_client) -> None:
    client, _settings = data_client
    token = "e" * 32
    created = client.post(
        f"{BASE}/cameras",
        headers=HEADERS,
        json={
            "camera_id": "cam-001",
            "name": "Entrance",
            "stream_path": "cam-001",
            "edge_device_id": "edge-001",
            "edge_management_url": "http://edge.test:8003",
            "edge_recovery_url": "http://edge.test:8002",
            "edge_auth_token": token,
        },
    )
    assert created.status_code == 201, created.text
    assert "edge_auth_token" not in created.json()

    target = client.get(
        f"{BASE}/cameras/cam-001/control-target", headers=HEADERS
    ).json()
    assert target["management_url"] == "http://edge.test:8003"
    assert target["auth_token"] == token

    profile = client.get(
        f"{BASE}/cameras/cam-001/video-profile", headers=HEADERS
    ).json()
    assert profile["current_profile"] == "hd"
    assert profile["desired_profile"] == "hd"
    changed = client.patch(
        f"{BASE}/cameras/cam-001/video-profile",
        headers=HEADERS,
        json={"desired_profile": "fhd"},
    ).json()
    assert changed["desired_profile"] == "fhd"
    assert changed["current_profile"] == "hd"

    first_status = client.put(
        f"{BASE}/cameras/cam-001/runtime-status",
        headers=HEADERS,
        json={
            "online": True,
            "cpu_percent": 12.5,
            "memory_percent": 34.5,
            "storage_percent": 56.5,
            "battery_percent": 78,
            "power_source": "battery",
            "camera_input": "online",
            "central_connection_status": "online",
            "current_video_profile": "hd",
            "last_seen_at": "2026-08-23T07:20:00Z",
        },
    )
    assert first_status.status_code == 200, first_status.text
    offline = client.put(
        f"{BASE}/cameras/cam-001/runtime-status",
        headers=HEADERS,
        json={"online": False, "last_error_code": "EDGE_OFFLINE"},
    )
    assert offline.status_code == 200, offline.text
    status_payload = client.get(
        f"{BASE}/cameras/cam-001/runtime-status", headers=HEADERS
    ).json()
    assert status_payload["online"] is False
    assert status_payload["cpu_percent"] == 12.5
    assert status_payload["camera_input"] == "online"
    assert status_payload["current_video_profile"] == "hd"

    rotated = client.patch(
        f"{BASE}/cameras/cam-001",
        headers=HEADERS,
        json={"edge_auth_token": "r" * 32},
    )
    assert rotated.status_code == 200, rotated.text
    rotated_target = client.get(
        f"{BASE}/cameras/cam-001/control-target", headers=HEADERS
    ).json()
    assert rotated_target["auth_token"] == "r" * 32
    assert rotated_target["management_url"] == target["management_url"]

    incomplete = client.patch(
        f"{BASE}/cameras/cam-001",
        headers=HEADERS,
        json={"edge_device_id": "edge-new"},
    )
    assert incomplete.status_code == 422

    assert client.delete(
        f"{BASE}/cameras/cam-001", headers=HEADERS
    ).status_code == 204
    assert client.get(
        f"{BASE}/edge-devices/edge-001", headers=HEADERS
    ).status_code == 404


def test_edge_service_urls_reject_ambiguous_or_credentialed_paths(data_client) -> None:
    client, _settings = data_client
    common = {
        "camera_id": "cam-001",
        "name": "Entrance",
        "stream_path": "cam-001",
        "edge_device_id": "edge-001",
        "edge_recovery_url": "http://edge.test:8002",
        "edge_auth_token": "e" * 32,
    }
    for invalid in (
        "http://user:pass@edge.test:8003",
        "http://edge.test:8003/control?token=secret",
        "http://edge.test:8003/a/../control",
    ):
        response = client.post(
            f"{BASE}/cameras",
            headers=HEADERS,
            json={**common, "edge_management_url": invalid},
        )
        assert response.status_code == 422


def test_edge_event_idempotency_recovery_lifecycle_and_crash_requeue(
    data_client,
) -> None:
    client, _settings = data_client
    response = client.post(
        f"{BASE}/cameras",
        headers=HEADERS,
        json={
            "camera_id": "cam-001",
            "name": "Entrance",
            "stream_path": "cam-001",
            "edge_device_id": "edge-001",
            "edge_management_url": "http://edge.test:8003",
            "edge_recovery_url": "http://edge.test:8002",
            "edge_auth_token": "e" * 32,
        },
    )
    assert response.status_code == 201, response.text
    base = datetime(2026, 8, 22, 7, 30, tzinfo=UTC)
    lost_payload = {
        "camera_id": "cam-001",
        "event_type": "central_connection_lost",
        "occurred_at": base.isoformat(),
        "edge_event_id": "edge-001:lost-1",
    }
    first = client.post(f"{BASE}/events", headers=HEADERS, json=lost_payload)
    replay = client.post(f"{BASE}/events", headers=HEADERS, json=lost_payload)
    assert first.status_code == replay.status_code == 201
    assert first.json()["id"] == replay.json()["id"]
    jobs = client.get(f"{BASE}/recovery-jobs", headers=HEADERS).json()["items"]
    assert len(jobs) == 1
    assert jobs[0]["status"] == "detected"

    restored = client.post(
        f"{BASE}/events",
        headers=HEADERS,
        json={
            "camera_id": "cam-001",
            "event_type": "central_connection_restored",
            "occurred_at": (base + timedelta(minutes=2)).isoformat(),
            "edge_event_id": "edge-001:restored-1",
        },
    )
    assert restored.status_code == 201, restored.text
    job = client.get(f"{BASE}/recovery-jobs", headers=HEADERS).json()["items"][0]
    assert job["status"] == "waiting_for_recovery"

    # A delayed duplicate reporter inside the same interval must not enqueue a
    # second transfer job.
    delayed = dict(lost_payload)
    delayed["edge_event_id"] = "inference:lost-duplicate"
    delayed["occurred_at"] = (base + timedelta(seconds=1)).isoformat()
    assert client.post(
        f"{BASE}/events", headers=HEADERS, json=delayed
    ).status_code == 201
    assert len(
        client.get(f"{BASE}/recovery-jobs", headers=HEADERS).json()["items"]
    ) == 1

    repository = client.app.state.repository
    claimed = repository.claim_due_recovery_job()
    assert claimed is not None
    assert claimed["status"] == "downloading"
    assert claimed["attempt_count"] == 1
    assert repository.requeue_interrupted_recovery_jobs() == 1
    requeued = repository.get_recovery_job(int(claimed["id"]))
    assert requeued is not None
    assert requeued["status"] == "failed"
    assert requeued["last_error"] == "RECOVERY_INTERRUPTED"
    assert requeued["next_retry_at"] is not None


def test_recovery_merges_out_of_order_reporter_boundaries(data_client) -> None:
    client, _settings = data_client
    _create_camera(client, "cam-001")
    base = datetime(2026, 8, 22, 7, 30, tzinfo=UTC)

    def event(event_id: str, event_type: str, occurred_at: datetime) -> None:
        response = client.post(
            f"{BASE}/events",
            headers=HEADERS,
            json={
                "camera_id": "cam-001",
                "event_type": event_type,
                "occurred_at": occurred_at.isoformat(),
                "edge_event_id": event_id,
            },
        )
        assert response.status_code == 201, response.text

    # Transport ordering is intentionally reversed: restore is persisted first.
    event(
        "inference:restored-early",
        "central_connection_restored",
        base + timedelta(minutes=2),
    )
    event(
        "edge:lost-late",
        "central_connection_lost",
        base + timedelta(seconds=10),
    )
    event("edge:lost-earlier", "central_connection_lost", base)
    repository = client.app.state.repository
    claimed = repository.claim_due_recovery_job()
    assert claimed is not None
    stale_revision = int(claimed["revision"])
    event(
        "edge:restored-later",
        "central_connection_restored",
        base + timedelta(minutes=2, seconds=20),
    )

    jobs = client.get(f"{BASE}/recovery-jobs", headers=HEADERS).json()["items"]
    assert len(jobs) == 1
    job = jobs[0]
    assert job["status"] == "waiting_for_recovery"
    assert datetime.fromisoformat(
        job["outage_started_at"].replace("Z", "+00:00")
    ) == base
    assert datetime.fromisoformat(
        job["outage_ended_at"].replace("Z", "+00:00")
    ) == base + timedelta(minutes=2, seconds=20)
    assert job["revision"] == 2
    assert repository.update_recovery_job(
        int(job["id"]),
        status="completed",
        expected_revision=stale_revision,
    ) is None
    assert repository.get_recovery_job(int(job["id"]))["status"] == (
        "waiting_for_recovery"
    )

    # A later distinct outage starts a new detected interval.
    event(
        "edge:lost-new",
        "central_connection_lost",
        base + timedelta(minutes=5),
    )
    jobs = client.get(f"{BASE}/recovery-jobs", headers=HEADERS).json()["items"]
    assert len(jobs) == 2
    assert {item["status"] for item in jobs} == {
        "waiting_for_recovery",
        "detected",
    }
    event(
        "edge:lost-old-delayed",
        "central_connection_lost",
        base + timedelta(minutes=1),
    )
    jobs = client.get(f"{BASE}/recovery-jobs", headers=HEADERS).json()["items"]
    assert len(jobs) == 2
    detected = next(item for item in jobs if item["status"] == "detected")
    assert datetime.fromisoformat(
        detected["outage_started_at"].replace("Z", "+00:00")
    ) == base + timedelta(minutes=5)


def test_recovery_waits_for_final_edge_segment_to_settle(data_client) -> None:
    client, _settings = data_client
    _create_camera(client, "cam-001")
    base = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=1)

    for event_id, event_type, occurred_at in (
        ("edge:lost", "central_connection_lost", base),
        (
            "edge:restored",
            "central_connection_restored",
            base + timedelta(seconds=20),
        ),
    ):
        response = client.post(
            f"{BASE}/events",
            headers=HEADERS,
            json={
                "camera_id": "cam-001",
                "event_type": event_type,
                "occurred_at": occurred_at.isoformat(),
                "edge_event_id": event_id,
            },
        )
        assert response.status_code == 201, response.text

    repository = client.app.state.repository
    job = repository.list_recovery_jobs("cam-001", 10, 0)[0]
    assert datetime.fromisoformat(
        job["next_retry_at"].replace("Z", "+00:00")
    ) == base + timedelta(seconds=35)
    assert repository.claim_due_recovery_job() is None

    later_restore = client.post(
        f"{BASE}/events",
        headers=HEADERS,
        json={
            "camera_id": "cam-001",
            "event_type": "central_connection_restored",
            "occurred_at": (base + timedelta(seconds=30)).isoformat(),
            "edge_event_id": "inference:restored-later",
        },
    )
    assert later_restore.status_code == 201
    job = repository.list_recovery_jobs("cam-001", 10, 0)[0]
    assert datetime.fromisoformat(
        job["next_retry_at"].replace("Z", "+00:00")
    ) == base + timedelta(seconds=45)
    assert repository.claim_due_recovery_job() is None


def test_legacy_network_events_are_stored_without_recovery_side_effects(
    data_client,
) -> None:
    client, _settings = data_client
    _create_camera(client, "cam-001")
    base = datetime(2026, 8, 22, 7, 30, tzinfo=UTC)

    def event(event_id: str, event_type: str, occurred_at: datetime) -> None:
        response = client.post(
            f"{BASE}/events",
            headers=HEADERS,
            json={
                "camera_id": "cam-001",
                "event_type": event_type,
                "occurred_at": occurred_at.isoformat(),
                "edge_event_id": event_id,
            },
        )
        assert response.status_code == 201, response.text

    # Legacy inference-consumer aliases remain searchable event history, but
    # cannot create an Edge segment-recovery interval by themselves.
    event("legacy:lost-only", "network_failure", base)
    event("legacy:restored-only", "network_recovery", base + timedelta(minutes=5))
    repository = client.app.state.repository
    assert repository.list_recovery_jobs("cam-001", 10, 0) == []

    event("edge:lost", "central_connection_lost", base + timedelta(minutes=1))
    detected = repository.list_recovery_jobs("cam-001", 10, 0)
    assert len(detected) == 1
    assert detected[0]["status"] == "detected"
    assert detected[0]["outage_ended_at"] is None
    event(
        "edge:restored",
        "central_connection_restored",
        base + timedelta(minutes=2),
    )
    before = repository.list_recovery_jobs("cam-001", 10, 0)
    assert len(before) == 1

    # Nor may delayed legacy aliases expand an authoritative Edge interval.
    event("legacy:lost-earlier", "network_failure", base - timedelta(minutes=1))
    event(
        "legacy:restored-later",
        "network_recovery",
        base + timedelta(minutes=3),
    )
    after = repository.list_recovery_jobs("cam-001", 10, 0)
    assert len(after) == 1
    assert after[0]["outage_started_at"] == before[0]["outage_started_at"]
    assert after[0]["outage_ended_at"] == before[0]["outage_ended_at"]
    assert after[0]["revision"] == before[0]["revision"]

    stored = repository.search_events(
        camera_id="cam-001",
        event_type=None,
        start_time=None,
        end_time=None,
        limit=20,
        offset=0,
    )
    assert {item["edge_event_id"] for item in stored} >= {
        "legacy:lost-only",
        "legacy:restored-only",
        "legacy:lost-earlier",
        "legacy:restored-later",
    }


def test_recording_content_supports_mpegts_range_requests(data_client) -> None:
    client, settings = data_client
    _create_camera(client, "cam-001")
    relative_path = "cam-001/recovered/segment.ts"
    content = b"\x47recovered-mpegts-content"
    target = settings.storage_root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    start = datetime(2026, 8, 23, 7, 30, tzinfo=UTC)
    created = client.post(
        f"{BASE}/recording-segments",
        headers=HEADERS,
        json={
            "camera_id": "cam-001",
            "start_time": start.isoformat(),
            "end_time": (start + timedelta(seconds=10)).isoformat(),
            "relative_path": relative_path,
            "format": "mpegts",
            "source": "edge_recovery",
        },
    )
    assert created.status_code == 201, created.text
    segment_id = created.json()["id"]

    response = client.get(
        f"{BASE}/recording-segments/{segment_id}/content",
        headers={**HEADERS, "Range": "bytes=1-5"},
    )
    assert response.status_code == 206
    assert response.content == content[1:6]
    assert response.headers["content-range"] == f"bytes 1-5/{len(content)}"
    assert response.headers["content-type"].startswith("video/mp2t")

    etag = client.get(
        f"{BASE}/recording-segments/{segment_id}/content",
        headers=HEADERS,
    ).headers["etag"]
    stale_validator = client.get(
        f"{BASE}/recording-segments/{segment_id}/content",
        headers={**HEADERS, "Range": "bytes=1-5", "If-Range": '"stale"'},
    )
    matching_validator = client.get(
        f"{BASE}/recording-segments/{segment_id}/content",
        headers={**HEADERS, "Range": "bytes=1-5", "If-Range": etag},
    )
    assert stale_validator.status_code == 200
    assert stale_validator.content == content
    assert matching_validator.status_code == 206
    assert matching_validator.content == content[1:6]


def test_camera_limit_history_delete_and_permission_replace_are_transactional(
    data_client,
) -> None:
    client, settings = data_client
    user = _create_user(client, "operator")
    for index in range(1, 5):
        _create_camera(client, f"cam-00{index}")
    limited = client.post(
        f"{BASE}/cameras",
        headers=HEADERS,
        json={
            "camera_id": "cam-005",
            "name": "cam-005",
            "stream_path": "cam-005",
        },
    )
    assert limited.status_code == 409
    assert limited.json()["error"]["code"] == "CAMERA_LIMIT_REACHED"

    disabled = client.patch(
        f"{BASE}/cameras/cam-001",
        headers=HEADERS,
        json={"enabled": False, "status": "disabled"},
    )
    assert disabled.status_code == 200
    replacement = client.post(
        f"{BASE}/cameras",
        headers=HEADERS,
        json={
            "camera_id": "cam-005",
            "name": "cam-005",
            "stream_path": "cam-005",
        },
    )
    assert replacement.status_code == 201
    over_limit_enable = client.patch(
        f"{BASE}/cameras/cam-001",
        headers=HEADERS,
        json={"enabled": True, "status": "offline"},
    )
    assert over_limit_enable.status_code == 409
    assert over_limit_enable.json()["error"]["code"] == "CAMERA_LIMIT_REACHED"

    # Restore this fixture's original four-camera set for history tests below.
    assert client.delete(f"{BASE}/cameras/cam-005", headers=HEADERS).status_code == 204
    assert client.patch(
        f"{BASE}/cameras/cam-001",
        headers=HEADERS,
        json={"enabled": True, "status": "offline"},
    ).status_code == 200

    replaced = client.put(
        f"{BASE}/users/{user['id']}/camera-permissions",
        headers=HEADERS,
        json={"camera_ids": ["cam-001"]},
    )
    assert replaced.status_code == 200
    assert [item["camera_id"] for item in replaced.json()["items"]] == [
        "cam-001"
    ]
    failed_replace = client.put(
        f"{BASE}/users/{user['id']}/camera-permissions",
        headers=HEADERS,
        json={"camera_ids": ["cam-002", "cam-missing"]},
    )
    assert failed_replace.status_code == 404
    unchanged = client.get(
        f"{BASE}/users/{user['id']}/camera-permissions", headers=HEADERS
    )
    assert [item["camera_id"] for item in unchanged.json()["items"]] == [
        "cam-001"
    ]

    start = datetime(2026, 8, 23, 8, 0, tzinfo=UTC)
    _create_segment(
        client,
        settings,
        camera_id="cam-001",
        relative_path="cam-001/history.mp4",
        start=start,
        end=start + timedelta(seconds=10),
    )
    deletion_status = client.get(
        f"{BASE}/cameras/cam-001/deletion-status", headers=HEADERS
    )
    assert deletion_status.status_code == 200
    assert deletion_status.json() == {
        "camera_id": "cam-001",
        "deletable": False,
        "reason_code": "CAMERA_HAS_HISTORY",
    }
    conflict = client.delete(f"{BASE}/cameras/cam-001", headers=HEADERS)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "CAMERA_HAS_HISTORY"
    assert client.get(
        f"{BASE}/cameras/cam-001", headers=HEADERS
    ).status_code == 200
