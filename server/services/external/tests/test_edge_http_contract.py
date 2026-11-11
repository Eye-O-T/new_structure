import tempfile
import time
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from ai_cctv_edge.config import EdgeConfig, render_toml, write_atomic
from ai_cctv_edge.control import ActivationResult, VideoCapabilities, create_control_app
from ai_cctv_edge.recovery import create_app as create_recovery_app
from ai_cctv_edge.segments import write_segment_metadata
from ai_cctv_edge.state import RuntimeStatusStore
from ai_cctv_edge.monitoring import PowerReading, ResourceSnapshot
from server.services.external.app.clients.edge import EdgeControlError, EdgeHttpClient


class Capabilities:
    def inspect(self, _config):
        return VideoCapabilities(("hd", "fhd"), True, True)


class ProfileRuntime:
    def __init__(self):
        self.current = "hd"
        self.persisted = "hd"
        self.requested = None

    def current_profile(self, default_profile):
        return self.current or default_profile

    def persisted_profile(self, default_profile):
        return self.persisted or default_profile

    def generation(self, _default_profile):
        return 0

    def preflight(self, _candidate, _timeout_seconds):
        return None

    def activate(self, profile, _generation):
        self.requested = profile
        self.current = profile

    def commit(self, profile, _generation):
        self.persisted = profile

    def clear_request(self, _generation):
        self.requested = None

    def wait_for(self, _profile, _generation, _timeout_seconds):
        return ActivationResult("applied")


class PowerSensor:
    def read(self):
        return PowerReading()


class Metrics:
    def sample(self):
        return ResourceSnapshot(1.0, 2.0, 3.0)


class EdgeHttpContractTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.config = EdgeConfig.load(
            Path(__file__).parents[4] / "edge" / "config" / "config.example.toml"
        )
        self.token = "edge-contract-test-token-" + "x" * 32
        self.token_file = root / "edge.token"
        write_atomic(self.token_file, self.token + "\n", mode=0o600)
        self.config = replace(
            self.config,
            backup=replace(self.config.backup, root=root / "recordings"),
            control=replace(
                self.config.control,
                token_file=self.token_file,
                apply_timeout_seconds=1,
                preflight_timeout_seconds=1,
            ),
            recovery=replace(self.config.recovery, token_file=self.token_file),
        )
        self.config_path = root / "config.toml"
        write_atomic(self.config_path, render_toml(self.config), mode=0o600)
        self.state_root = root / "state"
        self.runtime_root = root / "runtime"
        RuntimeStatusStore(self.state_root).write(
            {
                "camera_id": self.config.camera_id,
                "runner_pid": 0,
                "runner_instance_id": "contract-test",
                "state": "running",
                "updated_monotonic": time.monotonic(),
                "current_video_profile": "hd",
                "profile_generation": 0,
                "recording_requested": False,
                "central_connection_status": "unknown",
                "camera_input": "unknown",
            }
        )
        self.control_app = create_control_app(
            self.config_path,
            state_root=self.state_root,
            runtime_root=self.runtime_root,
            capability_probe=Capabilities(),
            profile_runtime=ProfileRuntime(),
            power_sensor=PowerSensor(),
            metrics=Metrics(),
        )

    async def test_server_client_status_capture_and_profile_contract(self):
        transport = httpx.ASGITransport(app=self.control_app)
        async with EdgeHttpClient(
            base_url="http://edge.test",
            auth_token=self.token,
            timeout_seconds=1,
            transport=transport,
        ) as edge:
            status = await edge.get_status()
            self.assertEqual(status["camera_id"], self.config.camera_id)
            self.assertIn("central_connection_status", status)

            accepted = await edge.set_capture_control(
                stream_enabled=False,
                recording_enabled=False,
            )
            self.assertFalse(accepted["stream_enabled"])
            self.assertFalse(accepted["recording_enabled"])

            capabilities = await edge.get_video_capabilities()
            self.assertEqual(capabilities["supported_profiles"], ["hd", "fhd"])
            changed = await edge.apply_video_profile("fhd")
            self.assertEqual(changed["status"], "applied")
            self.assertEqual(changed["current_profile"], "fhd")
            events = await edge.list_events(after=None, limit=50)
            self.assertEqual(events["camera_id"], self.config.camera_id)
            self.assertTrue(
                any(
                    item["event_type"] == "video_profile_changed"
                    for item in events["items"]
                )
            )

            with self.assertRaises(EdgeControlError) as rejected:
                await edge.apply_video_profile("4k")
            self.assertEqual(rejected.exception.code, "UNSUPPORTED_VIDEO_PROFILE")
            self.assertTrue(rejected.exception.profile_outcome_journaled)

    async def test_control_http_enforces_bearer_auth_and_request_validation(self):
        transport = httpx.ASGITransport(app=self.control_app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://edge.test",
        ) as client:
            unauthorized = await client.get("/internal/v1/status")
            self.assertEqual(unauthorized.status_code, 401)
            invalid_capture = await client.put(
                "/internal/v1/config/capture",
                headers={"Authorization": f"Bearer {self.token}"},
                json={"stream_enabled": False, "recording_enabled": True},
            )
            self.assertEqual(invalid_capture.status_code, 422)
            malformed_profile = await client.put(
                "/internal/v1/config/video-profile",
                headers={"Authorization": f"Bearer {self.token}"},
                json={},
            )
            self.assertEqual(malformed_profile.status_code, 422)

    async def test_recovery_http_returns_only_exact_overlapping_segments(self):
        camera_root = self.config.backup.root / self.config.camera_id
        segment = camera_root / "2026" / "11" / "11" / "20261111T010000.000000Z_000000.ts"
        segment.parent.mkdir(parents=True)
        segment.write_bytes(b"transport-stream")
        start = datetime(2026, 11, 11, 1, 0, tzinfo=UTC)
        end = start + timedelta(seconds=7, milliseconds=250)
        write_segment_metadata(
            segment,
            camera_id=self.config.camera_id,
            start_time=start,
            end_time=end,
        )
        (segment.parent / "legacy.ts").write_bytes(b"legacy without metadata")
        app = create_recovery_app(self.config_path)
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://edge.test",
        ) as client:
            unauthorized = await client.get(
                "/v1/recovery/manifest",
                params={"start": start.isoformat(), "end": end.isoformat()},
            )
            self.assertEqual(unauthorized.status_code, 401)
            headers = {"Authorization": f"Bearer {self.token}"}
            manifest = await client.get(
                "/v1/recovery/manifest",
                params={"start": start.isoformat(), "end": end.isoformat()},
                headers=headers,
            )
            self.assertEqual(manifest.status_code, 200)
            payload = manifest.json()
            self.assertEqual(len(payload["items"]), 1)
            item = payload["items"][0]
            self.assertEqual(item["start_time"], "2026-11-11T01:00:00Z")
            self.assertEqual(item["end_time"], "2026-11-11T01:00:07.250000Z")
            adjacent = await client.get(
                "/v1/recovery/manifest",
                params={
                    "start": end.isoformat(),
                    "end": (end + timedelta(seconds=1)).isoformat(),
                },
                headers=headers,
            )
            self.assertEqual(adjacent.json()["items"], [])
            recovered = await client.get(
                "/v1/recovery/files/" + item["relative_path"],
                headers=headers,
            )
            self.assertEqual(recovered.status_code, 200)
            self.assertEqual(recovered.content, b"transport-stream")


if __name__ == "__main__":
    unittest.main()

