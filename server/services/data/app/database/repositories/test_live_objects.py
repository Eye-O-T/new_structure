import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from ai_cctv_core.contracts.objects import LIVE_OBJECTS_MAX_AGE_SECONDS, LiveObjects

from ..connection import Database
from . import DataRepository


NOW = datetime(2026, 11, 9, 12, 0, 0, tzinfo=timezone.utc)
PERSON = {"person_id": "7", "bbox": [10, 20, 110, 220], "confidence": 0.9}


class LiveObjectsRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.repository = DataRepository(Database(Path(self.directory.name) / "data.sqlite"))
        self.repository.initialize()
        with self.repository.database.transaction() as connection:
            connection.execute(
                "INSERT INTO cameras(camera_id,name,stream_path,created_at,updated_at) "
                "VALUES('cam_001','Camera','cam_001','now','now')"
            )

    def tearDown(self):
        self.directory.cleanup()

    def _payload(self, observed_at, objects=None, session="a" * 32):
        return LiveObjects(
            tracking_session_id=session,
            observed_at=observed_at,
            frame_width=640,
            frame_height=480,
            objects=[PERSON] if objects is None else objects,
        )

    def _put(self, payload):
        with patch(
            "server.services.data.app.database.repositories.objects.utc_now",
            return_value=NOW,
        ):
            self.repository.put_live_objects("cam_001", payload)

    def _get(self):
        with patch(
            "server.services.data.app.database.repositories.objects.utc_now",
            return_value=NOW,
        ):
            return self.repository.get_live_objects("cam_001")

    def test_observation_older_than_expiry_is_hidden_even_when_received_now(self):
        self._put(self._payload(NOW - timedelta(seconds=5)))

        result = self._get()

        self.assertTrue(result["stale"])
        self.assertEqual(result["stale_reason"], "expired")
        self.assertEqual(result["objects"], [])
        self.assertEqual(result["received_age_seconds"], 0)

    def test_observation_at_expiry_boundary_is_still_fresh(self):
        self._put(
            self._payload(
                NOW - timedelta(seconds=LIVE_OBJECTS_MAX_AGE_SECONDS)
            )
        )

        result = self._get()

        self.assertFalse(result["stale"])
        self.assertEqual(result["objects"][0]["person_id"], "7")

    def test_future_observation_is_hidden(self):
        self._put(self._payload(NOW + timedelta(seconds=1)))

        result = self._get()

        self.assertTrue(result["stale"])
        self.assertEqual(result["stale_reason"], "future_timestamp")

    def test_missing_or_disabled_camera_has_no_live_observation(self):
        self.assertEqual(self._get()["stale_reason"], "no_observation")
        self._put(self._payload(NOW))
        with self.repository.database.transaction() as connection:
            connection.execute(
                "UPDATE cameras SET enabled=0 WHERE camera_id='cam_001'"
            )

        result = self._get()

        self.assertTrue(result["stale"])
        self.assertEqual(result["stale_reason"], "no_observation")
        self.assertEqual(result["objects"], [])

    def test_valid_empty_observation_refreshes_live_status(self):
        self._put(self._payload(NOW, objects=[]))

        result = self._get()

        self.assertFalse(result["stale"])
        self.assertEqual(result["objects"], [])

    def test_late_older_payload_does_not_replace_newer_observation(self):
        self._put(self._payload(NOW - timedelta(seconds=1)))
        self._put(
            self._payload(
                NOW - timedelta(seconds=2),
                objects=[{**PERSON, "person_id": "8"}],
            )
        )

        result = self._get()

        self.assertFalse(result["stale"])
        self.assertEqual(result["objects"][0]["person_id"], "7")


if __name__ == "__main__":
    unittest.main()
