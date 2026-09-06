"""Cross-domain event writes must remain atomic after repository separation."""

import sqlite3
from datetime import timedelta

import pytest

from ai_cctv_core.time import format_utc, utc_now
from server.services.data.app.database.connection import Database
from server.services.data.app.database.repositories import DataRepository


def test_object_queue_failure_rolls_back_event_push_and_identity_job(tmp_path):
    repository = DataRepository(Database(tmp_path / "events.db"))
    repository.initialize()
    user = repository.create_user(
        {"username": "operator", "role": "admin", "password_hash": "test-only"}
    )
    repository.create_camera(
        {"camera_id": "cam-001", "name": "Entrance", "stream_path": "cam-001"}
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
    event = {
        "camera_id": "cam-001",
        "event_type": "person_appeared",
        "occurred_at": format_utc(utc_now()),
        "person_id": "1",
        "object_observation": {},
    }
    with repository.database.transaction() as connection:
        connection.execute(
            "CREATE TRIGGER fail_analysis_queue BEFORE INSERT ON object_jobs "
            "WHEN NEW.stage = 'analysis' BEGIN "
            "SELECT RAISE(ABORT, 'analysis queue unavailable'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="analysis queue unavailable"):
        repository.create_event(event)

    with repository.database.connection() as connection:
        for table in ("events", "push_deliveries", "object_jobs"):
            assert (
                connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
            )
        connection.execute("DROP TRIGGER fail_analysis_queue")

    repository.create_event(event)
    with repository.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM events").fetchone()[0] == 1
        assert (
            connection.execute("SELECT count(*) FROM push_deliveries").fetchone()[0]
            == 1
        )
        assert connection.execute("SELECT count(*) FROM object_jobs").fetchone()[0] == 2
