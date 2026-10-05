import unittest

from server.setup.install_helper.server_api import ServerApiClient


class EdgeMacContractTests(unittest.TestCase):
    def setUp(self):
        self.client = ServerApiClient("https://server.example")
        self.client._access_token = "session"
        self.client._request = lambda method, path, payload: payload

    def test_register_edge_accepts_normalized_mac(self):
        result = self.client.register_edge(
            camera_id="cam-1",
            name="Camera",
            edge_device_id="edge-1",
            edge_mac_address="AA:BB:CC:DD:EE:FF",
            edge_management_url="http://192.0.2.10:8003",
            edge_recovery_url="http://192.0.2.10:8002",
            edge_auth_token="t" * 32,
        )
        self.assertEqual(result["edge_mac_address"], "aa:bb:cc:dd:ee:ff")

    def test_register_edge_rejects_empty_or_malformed_mac(self):
        for mac in ("", "aa:bb:cc:dd:ee", "not-a-mac"):
            with self.subTest(mac=mac), self.assertRaises(ValueError):
                self.client.register_edge(
                    camera_id="cam-1", name="Camera", edge_device_id="edge-1",
                    edge_mac_address=mac,
                    edge_management_url="http://192.0.2.10:8003",
                    edge_recovery_url="http://192.0.2.10:8002",
                    edge_auth_token="t" * 32,
                )


if __name__ == "__main__":
    unittest.main()
