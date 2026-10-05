import unittest

from .base import EdgeIdentityConflict
from .cameras import CamerasRepositoryMixin


class _Result:
    def __init__(self, row):
        self.row = row

    def fetchone(self):
        return self.row


class _Connection:
    def __init__(self, by_device=None, by_mac=None):
        self.rows = [by_device, by_mac]

    def execute(self, _sql, _args):
        return _Result(self.rows.pop(0))


class EdgeIdentityTests(unittest.TestCase):
    def check(self, device, mac, by_device=None, by_mac=None):
        return CamerasRepositoryMixin._check_edge_identity(
            _Connection(by_device, by_mac), device, mac
        )

    def test_new_identity(self):
        self.assertIsNone(self.check("edge-1", "aa:bb:cc:dd:ee:ff"))

    def test_matching_identity(self):
        row = {"edge_device_id": "edge-1", "mac_address": "aa:bb:cc:dd:ee:ff"}
        self.assertIs(self.check("edge-1", row["mac_address"], row), row)

    def test_device_and_mac_conflicts(self):
        row = {"edge_device_id": "edge-1", "mac_address": "aa:bb:cc:dd:ee:ff"}
        with self.assertRaises(EdgeIdentityConflict):
            self.check("edge-1", "11:22:33:44:55:66", row)
        with self.assertRaises(EdgeIdentityConflict):
            self.check("edge-2", row["mac_address"], None, row)


if __name__ == "__main__":
    unittest.main()
