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
