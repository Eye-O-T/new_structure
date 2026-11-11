import json
import unittest
from unittest.mock import patch

from server.edge_onboarding.discovery import DiscoveredEdge
from server.edge_onboarding.pairing import (
    EdgePairingError,
    probe_edge_connection,
    validate_pairing_settings,
)


class _Response:
    status = 200

    def __init__(self, payload):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit):
        return self.payload


class EdgePairingIdentityTests(unittest.TestCase):
    def setUp(self):
        self.edge = DiscoveredEdge(
            device_id="edge-1",
            camera_id="cam-1",
            address="192.0.2.10",
            management_url="http://192.0.2.10:8003",
            recovery_url="http://192.0.2.10:8002",
            supported_profiles=("hd",),
            message_id="message-1",
            sent_at=1,
            mac_address="aa:bb:cc:dd:ee:ff",
            advertised_ip="192.0.2.10",
        )

    def test_probe_returns_identity_reported_by_edge(self):
        payload = {"status": "pairing", "device_id": "edge-1", "camera_id": "cam-1"}
        opener = type("Opener", (), {"open": lambda *_args, **_kwargs: _Response(payload)})()
        with patch("server.edge_onboarding.pairing.build_opener", return_value=opener):
            result = probe_edge_connection(self.edge)
        self.assertEqual(result["device_id"], "edge-1")
        self.assertEqual(result["camera_id"], "cam-1")

    def test_probe_rejects_identity_different_from_discovery(self):
        payload = {"status": "pairing", "device_id": "other-edge", "camera_id": "cam-1"}
        opener = type("Opener", (), {"open": lambda *_args, **_kwargs: _Response(payload)})()
        with patch("server.edge_onboarding.pairing.build_opener", return_value=opener):
            with self.assertRaisesRegex(EdgePairingError, "identity"):
                probe_edge_connection(self.edge)

    def test_pairing_rejects_loopback_server_address(self):
        for host in ("127.0.0.1", "::1", "localhost"):
            with self.subTest(host=host), self.assertRaises(EdgePairingError):
                validate_pairing_settings(
                    self.edge,
                    central_host=host,
                    central_port=8554,
                    video_profile="hd",
                    backup_root="/var/lib/ai-cctv-edge/recordings",
                )


if __name__ == "__main__":
    unittest.main()
