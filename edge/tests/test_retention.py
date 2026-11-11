import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ai_cctv_edge.retention import enforce_retention
from ai_cctv_edge.segments import metadata_path, write_segment_metadata


class RetentionMetadataTests(unittest.TestCase):
    def test_deleting_segment_also_deletes_its_sidecar(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            segment = root / "old.ts"
            segment.write_bytes(b"old")
            sidecar = metadata_path(segment)
            sidecar.write_text("{}", encoding="utf-8")

            deleted = enforce_retention(
                root,
                max_bytes=1024,
                max_age_hours=1,
                now=time.time() + 7200,
            )

            self.assertEqual(deleted, [segment])
            self.assertFalse(segment.exists())
            self.assertFalse(sidecar.exists())

    def test_preserved_active_segment_keeps_sidecar_and_orphans_are_removed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            segment = root / "active.ts"
            segment.write_bytes(b"active")
            sidecar = metadata_path(segment)
            sidecar.write_text("{}", encoding="utf-8")
            orphan = metadata_path(root / "removed.ts")
            orphan.write_text("{}", encoding="utf-8")

            deleted = enforce_retention(
                root,
                max_bytes=1024,
                max_age_hours=1,
                now=time.time() + 7200,
                preserve_newest=True,
            )

            self.assertEqual(deleted, [])
            self.assertTrue(segment.exists())
            self.assertTrue(sidecar.exists())
            self.assertFalse(orphan.exists())

    def test_size_retention_deletes_sidecar_with_segment(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "first.ts"
            second = root / "second.ts"
            first.write_bytes(b"12345")
            second.write_bytes(b"67890")
            first_sidecar = metadata_path(first)
            second_sidecar = metadata_path(second)
            first_sidecar.write_text("{}", encoding="utf-8")
            second_sidecar.write_text("{}", encoding="utf-8")

            deleted = enforce_retention(
                root,
                max_bytes=5,
                max_age_hours=24,
                now=time.time() + 7200,
                preserve_newest=True,
            )

            self.assertEqual(deleted, [first])
            self.assertFalse(first_sidecar.exists())
            self.assertTrue(second_sidecar.exists())

    def test_age_retention_uses_segment_end_time_in_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            segment = root / "old-by-media-time.ts"
            segment.write_bytes(b"old")
            now = datetime.now(UTC)
            start = now - timedelta(hours=3)
            write_segment_metadata(
                segment,
                camera_id="cam-001",
                start_time=start,
                end_time=start + timedelta(minutes=1),
            )

            deleted = enforce_retention(
                root,
                max_bytes=1024,
                max_age_hours=1,
                now=now.timestamp(),
                camera_id="cam-001",
            )

            self.assertEqual(deleted, [segment])
            self.assertFalse(metadata_path(segment).exists())


if __name__ == "__main__":
    unittest.main()

