import threading
import unittest
from unittest.mock import patch

from ai_cctv_edge.network import network_details
from ai_cctv_edge.pairing import advertise_until_stopped


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

    @patch("ai_cctv_edge.pairing.interface_ipv4")
    @patch("ai_cctv_edge.pairing.socket.socket")
    def test_interface_selects_source_and_broadcast(self, socket_factory, lookup):
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
            pairing_key="x" * 32,
            interval_seconds=1,
            interface="eth0",
        )
        socket_instance.bind.assert_called_once_with(("192.168.50.2", 0))
        self.assertEqual(socket_instance.sendto.call_args.args[1], ("192.168.50.255", 37020))

    @patch("ai_cctv_edge.pairing.socket.socket")
    def test_without_interface_keeps_global_broadcast(self, socket_factory):
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
            pairing_key="x" * 32,
            interval_seconds=1,
        )
        socket_instance.bind.assert_called_once_with(("0.0.0.0", 0))
        self.assertEqual(socket_instance.sendto.call_args.args[1], ("255.255.255.255", 37020))


if __name__ == "__main__":
    unittest.main()
