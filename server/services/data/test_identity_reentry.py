"""Regression tests for same-camera identity reuse and concurrent tracks."""

import importlib.util
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "lib"))

from ai_cctv_core.contracts.objects import IdentityDescriptor
from ai_cctv_core.time import format_utc


def _load_database_module(name: str, path: Path):
    # Keep these focused database tests runnable without starting the FastAPI app.
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


DATABASE_ROOT = Path(__file__).parent / "app" / "database"
Database = _load_database_module(
    "data_connection_under_test", DATABASE_ROOT / "connection.py"
).Database
identity = _load_database_module(
    "data_identity_under_test", DATABASE_ROOT / "repositories" / "identity.py"
)

CAMERA_ID = "local-camera"
SESSION = "a" * 32
BASE_TIME = datetime(2026, 10, 8, tzinfo=UTC)
SAME_PERSON = IdentityDescriptor(
    space_id="osnet:controlled-test", features=[1.0] + [0.0] * 511
)
DIFFERENT_PERSON = IdentityDescriptor(
    space_id="osnet:controlled-test", features=[0.0, 1.0] + [0.0] * 510
)


class IdentityReentryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        database = Database(Path(directory.name) / "identity.sqlite3")
        database.initialize()
        self.connection = database.connect()
        self.addCleanup(self.connection.close)
        self.connection.execute(
            "INSERT INTO cameras(camera_id,name,stream_path,created_at,updated_at) "
            "VALUES (?,?,?,?,?)",
            (CAMERA_ID, "Local Camera", CAMERA_ID, self._at(0), self._at(0)),
        )

    @staticmethod
    def _at(seconds: int) -> str:
        return format_utc(BASE_TIME + timedelta(seconds=seconds))

    def _end(self, person_id: str, seconds: int, *, session: str = SESSION):
        identity.note_track_observation(
            self.connection, CAMERA_ID, session, person_id, self._at(seconds), ended=True
        )

    def _resolve(
        self,
        person_id: str,
        seconds: int,
        *,
        session: str = SESSION,
        descriptor: IdentityDescriptor = SAME_PERSON,
    ):
        observed_at = self._at(seconds)
        identity.note_track_observation(
            self.connection, CAMERA_ID, session, person_id, observed_at
        )
        row = self.connection.execute(
            "SELECT ? AS camera_id, ? AS person_id, ? AS occurred_at",
            (CAMERA_ID, person_id, observed_at),
        ).fetchone()
        global_id, match = identity.resolve_identity(
            self.connection, row, session, descriptor, observed_at
        )
        self.connection.execute(
            "INSERT OR IGNORE INTO person_identity_links VALUES (?,?,?,?,?)",
            (CAMERA_ID, session, person_id, global_id, observed_at),
        )
        return global_id, match

    def test_same_track_reentry_keeps_existing_global_id(self):
        first_id, first = self._resolve("1", 0)
        self._end("1", 4)
        return_id, returned = self._resolve("1", 8)

        self.assertEqual(first["decision"], "new")
        self.assertEqual(returned["decision"], "existing_track")
        self.assertIsNone(returned["similarity"])
        self.assertEqual(return_id, first_id)

    def test_new_track_within_30_seconds_reuses_global_id(self):
        first_id, _ = self._resolve("1", 0)
        self._end("1", 4)
        return_id, returned = self._resolve("2", 20)

        self.assertEqual(returned["decision"], "matched")
        self.assertEqual(returned["similarity"], 1.0)
        self.assertEqual(returned["threshold"], 0.97)
        self.assertEqual(returned["margin"], 0.05)
        self.assertEqual(return_id, first_id)

    def test_new_track_at_30_second_boundary_reuses_global_id(self):
        first_id, _ = self._resolve("1", 0)
        self._end("1", 4)
        return_id, returned = self._resolve("2", 30)

        self.assertEqual(returned["decision"], "matched")
        self.assertEqual(return_id, first_id)

    def test_new_track_after_30_seconds_reuses_global_id(self):
        first_id, _ = self._resolve("1", 0)
        self._end("1", 4)
        return_id, returned = self._resolve("2", 40)

        self.assertEqual(returned["decision"], "matched")
        self.assertEqual(returned["similarity"], 1.0)
        self.assertEqual(return_id, first_id)

    def test_new_tracking_session_can_reuse_global_id(self):
        first_id, _ = self._resolve("1", 0)
        self._end("1", 4)
        return_id, returned = self._resolve("1", 20, session="b" * 32)

        self.assertEqual(returned["decision"], "matched")
        self.assertEqual(return_id, first_id)
        self.assertEqual(
            self.connection.execute(
                "SELECT COUNT(*) FROM person_identity_links WHERE camera_id=?",
                (CAMERA_ID,),
            ).fetchone()[0],
            2,
        )

    def test_overlapping_tracks_cannot_share_global_id(self):
        first_id, _ = self._resolve("1", 0)
        identity.note_track_observation(
            self.connection, CAMERA_ID, SESSION, "1", self._at(25)
        )
        second_id, second = self._resolve("2", 20)

        self.assertEqual(second["decision"], "new")
        self.assertIsNone(second["similarity"])
        self.assertNotEqual(second_id, first_id)

    def test_dissimilar_new_track_gets_new_global_id(self):
        first_id, _ = self._resolve("1", 0)
        self._end("1", 4)
        second_id, second = self._resolve("2", 20, descriptor=DIFFERENT_PERSON)

        self.assertEqual(second["decision"], "new")
        self.assertEqual(second["similarity"], 0.0)
        self.assertNotEqual(second_id, first_id)


if __name__ == "__main__":
    unittest.main()
