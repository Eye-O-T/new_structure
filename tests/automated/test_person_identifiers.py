# 기존 DB의 인물 식별자 이전과 공개 응답의 로컬·통합 ID 구분을 확인한다.
import json
import shutil
from pathlib import Path

from server.services.data.app.database.connection import Database
from server.services.external.app.schemas import EventResponse


# 005 이전 스키마에 혼합된 옛 ID를 넣고 이전·재시작 후 보존과 재실행 안전성을 검사한다.
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


# 서로 다른 카메라의 로컬 ID는 달라도 서버가 연결한 통합 ID는 공유할 수 있다.
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
