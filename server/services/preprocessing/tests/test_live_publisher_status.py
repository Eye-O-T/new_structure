import threading
import unittest
from datetime import datetime, timedelta, timezone

from ai_cctv_core.time import format_utc

from server.services.preprocessing.app.live_publisher import LivePublisher


class _DataClient:
    def __init__(self, error=None):
        self.error = error
        self.finished = threading.Event()

    def put_live_objects(self, _camera_id, _payload):
        try:
            if self.error:
                raise self.error
            return {}
        finally:
            self.finished.set()


class LivePublisherStatusTests(unittest.TestCase):
    def _publish(self, observed_at, error=None):
        client = _DataClient(error)
        publisher = LivePublisher(client, "cam_001")
        publisher.submit({"observed_at": format_utc(observed_at), "objects": []})
        self.assertTrue(client.finished.wait(timeout=2))
        publisher.close()
        return publisher.status()

    def test_recent_empty_frame_is_successful_and_fresh(self):
        result = self._publish(datetime.now(timezone.utc))

        self.assertIsNone(result["last_error_code"])
        self.assertEqual(result["consecutive_failures"], 0)
        self.assertFalse(result["stale"])
        self.assertGreaterEqual(result["observed_age_seconds"], 0)

    def test_old_frame_is_stale_immediately_after_successful_delivery(self):
        result = self._publish(
            datetime.now(timezone.utc) - timedelta(seconds=5)
        )

        self.assertTrue(result["stale"])
        self.assertGreater(result["observed_age_seconds"], 3)

    def test_failed_delivery_does_not_mark_a_frame_fresh(self):
        result = self._publish(
            datetime.now(timezone.utc), error=TimeoutError("synthetic timeout")
        )

        self.assertEqual(result["last_error_code"], "TIMEOUT")
        self.assertEqual(result["consecutive_failures"], 1)
        self.assertIsNone(result["observed_age_seconds"])
        self.assertFalse(result["stale"])


if __name__ == "__main__":
    unittest.main()
