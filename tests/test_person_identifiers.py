import json
import shutil
from pathlib import Path

from server.services.data.app.database.connection import Database
from server.services.external.app.schemas import EventResponse


def test_existing_database_preserves_local_ids_without_inventing_global_ids(tmp_path):
    migrations = Path("server/services/data/app/database/migrations")
    old_migrations = tmp_path / "old_migrations"
    old_migrations.mkdir()
    for migration in sorted(migrations.glob("*.sql")):
        if migration.name < "005":
            shutil.copy2(migration, old_migrations / migration.name)
    path = tmp_path / "existing.db"
    database = Database(path, migrations_dir=old_migrations)
    database.initialize()
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO cameras(camera_id,name,stream_path,created_at,updated_at) VALUES ('cam-001','Camera','cam-001','now','now')"
        )
        for person_id, track_id in [
            ("7", "7"),
            (None, "8"),
            ("9", None),
            ("old-value", "10"),
            (None, None),
        ]:
            connection.execute(
                "INSERT INTO events(camera_id,event_type,occurred_at,person_id,track_id,created_at) VALUES ('cam-001','person_appeared','2026-09-06T00:00:00Z',?,?, '2026-09-06T00:00:00Z')",
                (person_id, track_id),
            )
    database = Database(path)
    database.initialize()
    database.initialize()  # A restart must not apply the migration twice.
    with database.connection() as connection:
        rows = connection.execute("SELECT * FROM events ORDER BY id").fetchall()
        assert [row["person_id"] for row in rows] == ["7", "8", "9", "10", None]
        assert all(row["global_person_id"] is None for row in rows)
        assert "track_id" not in rows[0].keys()
        assert json.loads(rows[3]["metadata_json"])["legacy_person_id"] == "old-value"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_public_events_keep_local_and_global_identifiers_separate():
    shared = dict(
        id=1, event_type="person_appeared", occurred_at="2026-09-06T00:00:00Z"
    )
    first = EventResponse(
        **shared, camera_id="cam-001", person_id="7", global_person_id="global-1"
    )
    second = EventResponse(
        **shared, camera_id="cam-002", person_id="12", global_person_id="global-1"
    )
    assert first.person_id != second.person_id
    assert first.global_person_id == second.global_person_id
    assert "track_id" not in first.model_dump()
    assert (
        EventResponse(**shared, camera_id="cam-003", person_id="7").global_person_id
        is None
    )
