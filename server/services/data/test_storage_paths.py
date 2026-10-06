import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "lib"))

from server.services.data.app.errors import ApiError
from server.services.data.app.storage.paths import normalize_relative_path


class SnapshotPathTests(unittest.TestCase):
    def test_rejects_paths_outside_snapshot_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for value in (
                "../secret.jpg",
                "../../etc/passwd",
                "/etc/passwd",
                r"C:\Windows\system.ini",
                r"\\server\share\file",
            ):
                with self.subTest(value=value), self.assertRaises(ApiError):
                    normalize_relative_path(root, value)

    def test_resolves_a_path_inside_snapshot_root(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(
                normalize_relative_path(root, "events/image.jpg"),
                ("events/image.jpg", root / "events" / "image.jpg"),
            )


if __name__ == "__main__":
    unittest.main()
