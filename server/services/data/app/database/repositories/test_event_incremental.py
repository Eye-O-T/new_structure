import tempfile
import unittest
from pathlib import Path

from ..connection import Database
from . import DataRepository


class IncrementalEventTests(unittest.TestCase):
    def test_late_and_sparse_event_ids_are_ordered_by_storage_id(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = DataRepository(Database(Path(directory) / "data.sqlite"))
            repository.initialize()
            with repository.database.transaction() as connection:
                connection.execute(
                    "INSERT INTO cameras(camera_id,name,stream_path,created_at,updated_at) "
                    "VALUES('cam_001','Camera','cam_001','now','now')"
                )
                for event_id, occurred_at in ((1, "2026-11-03T08:00:00Z"), (3, "2026-11-03T07:00:00Z")):
                    connection.execute(
                        "INSERT INTO events(id,camera_id,event_type,occurred_at,created_at) "
                        "VALUES(?, 'cam_001','person_appeared',?, 'now')",
                        (event_id, occurred_at),
                    )

            late = repository.search_events(
                camera_id="cam_001", event_type=None, start_time=None,
                end_time=None, limit=10, offset=0, snapshot_max_id=3,
                after_event_id=1,
            )
            self.assertEqual([int(item["id"]) for item in late], [3])
            self.assertEqual(late[0]["occurred_at"], "2026-11-03T07:00:00Z")


if __name__ == "__main__":
    unittest.main()
