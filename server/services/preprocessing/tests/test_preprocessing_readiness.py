import unittest
from types import SimpleNamespace
from unittest.mock import patch

from server.services.preprocessing.app.main import app


class PreprocessingReadinessTests(unittest.TestCase):
    def setUp(self):
        self.original_supervisor = app.state.supervisor
        self.original_identity_worker = app.state.identity_worker
        self.original_identity_error = app.state.identity_error
        app.state.identity_worker = SimpleNamespace(
            ready=True,
            stalled=False,
            last_error=None,
            last_outcome="complete",
            plugin=SimpleNamespace(runtime={}),
        )
        app.state.identity_error = None

    def tearDown(self):
        app.state.supervisor = self.original_supervisor
        app.state.identity_worker = self.original_identity_worker
        app.state.identity_error = self.original_identity_error

    def _supervisor(self, *, stale, age):
        return SimpleNamespace(
            status=lambda: {
                "data_ready": True,
                "workers": {
                    "cam_001": {
                        "state": "online",
                        "model_ready": True,
                        "live_objects_observed_age_seconds": age,
                        "live_objects_stale": stale,
                    }
                },
                "stopped_workers": {},
                "event_delivery": {
                    "pending": 0,
                    "rejected": 0,
                    "waiting": 0,
                    "last_error": None,
                },
            }
        )

    def _health_ready(self, *, stale, age):
        app.state.supervisor = self._supervisor(stale=stale, age=age)
        route = next(
            route.endpoint for route in app.routes
            if getattr(route, "path", None) == "/health/ready"
        )
        with patch.dict("os.environ", {"PREPROCESSING_GPU": "false"}):
            return route()

    def test_expired_coordinates_degrade_readiness(self):
        result = self._health_ready(stale=True, age=4.0)

        self.assertEqual(result["status"], "degraded")
        self.assertTrue(result["workers"]["cam_001"]["live_objects_stale"])

    def test_recent_coordinates_keep_readiness_ready(self):
        result = self._health_ready(stale=False, age=1.0)

        self.assertEqual(result["status"], "ready")


if __name__ == "__main__":
    unittest.main()
