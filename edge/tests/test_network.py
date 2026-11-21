import threading
import unittest
from unittest.mock import patch

from ai_cctv_edge.network import network_details
from ai_cctv_edge.config import (
    BackupConfig, ControlConfig, EdgeConfig, NetworkConfig, RecoveryConfig,
    RtspConfig, VideoConfig, render_toml,
)
from ai_cctv_edge.network_policy import render_ruleset

try:
    from ai_cctv_edge.pairing import advertise_until_stopped
except ModuleNotFoundError as exc:
    if exc.name != "fastapi":
        raise
    advertise_until_stopped = None


class NetworkTests(unittest.TestCase):
    def test_directed_broadcast(self):
        result = network_details("eth0", "192.168.50.2", "255.255.255.0")
        self.assertEqual((result.address, result.prefix, result.broadcast),
                         ("192.168.50.2", 24, "192.168.50.255"))

    def test_invalid_interface_is_rejected(self):
        from unittest.mock import patch
        from ai_cctv_edge.network import interface_ipv4

        with patch("ai_cctv_edge.network.socket.if_nametoindex", side_effect=OSError):
            with self.assertRaisesRegex(ValueError, "does not exist"):
                interface_ipv4("missing0")

    @unittest.skipIf(advertise_until_stopped is None, "FastAPI is not installed")
    def test_interface_selects_source_and_broadcast(self):
        with (
            patch("ai_cctv_edge.pairing.interface_mac", return_value="02:00:00:00:00:01"),
            patch("ai_cctv_edge.pairing.interface_ipv4") as lookup,
            patch("ai_cctv_edge.pairing.socket.socket") as socket_factory,
        ):
            lookup.return_value = network_details("eth0", "192.168.50.2", "255.255.255.0")
            socket_instance = socket_factory.return_value.__enter__.return_value
            stop = threading.Event()
            socket_instance.sendto.side_effect = lambda *_args: stop.set()
            advertise_until_stopped(
                stop,
                device_id="edge-001",
                camera_id="cam-001",
                management_port=8003,
                recovery_port=8002,
                supported_profiles=("hd",),
                interval_seconds=1,
                interface="eth0",
            )
            socket_instance.bind.assert_called_once_with(("192.168.50.2", 0))
            self.assertEqual(socket_instance.sendto.call_args.args[1], ("192.168.50.255", 37020))

    @unittest.skipIf(advertise_until_stopped is None, "FastAPI is not installed")
    def test_without_interface_fails_before_socket_creation(self):
        with patch("ai_cctv_edge.pairing.socket.socket") as socket_factory:
            with self.assertRaisesRegex(ValueError, "interface"):
                advertise_until_stopped(
                    threading.Event(),
                    device_id="edge-001",
                    camera_id="cam-001",
                    management_port=8003,
                    recovery_port=8002,
                    supported_profiles=("hd",),
                    interval_seconds=1,
                )
            socket_factory.assert_not_called()

    def test_config_serializes_selected_interface(self):
        config = EdgeConfig(
            schema_version=1,
            device_id="edge-001",
            camera_id="cam-001",
            video=VideoConfig.from_profile("hd"),
            rtsp=RtspConfig(),
            backup=BackupConfig(),
            recovery=RecoveryConfig(),
            control=ControlConfig(),
            network=NetworkConfig("eth0"),
        )
        self.assertIn('interface = "eth0"', render_toml(config))

    def test_policy_scopes_service_input_and_output(self):
        policy = render_ruleset(interface="eth0", ports=(8002, 8003), service_uid=1001)
        self.assertIn('iifname "eth0" tcp dport { 8002, 8003 } accept', policy)
        self.assertIn('meta skuid 1001 meta nfproto ipv4 oifname != "eth0" drop', policy)
        self.assertIn('ct mark & 0x40000000 != 0 iifname != "eth0" drop', policy)



if __name__ == "__main__":
    unittest.main()
