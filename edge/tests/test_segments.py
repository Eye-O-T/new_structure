import json
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ai_cctv_edge.segments import (
    metadata_path,
    read_segment_metadata,
    segment_overlaps,
    write_segment_metadata,
)


class SegmentMetadataTests(unittest.TestCase):
    def test_writes_atomic_complete_utc_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            segment = Path(temporary) / "recording.ts"
            start = datetime(2026, 11, 11, 1, 0, tzinfo=UTC)
            target = write_segment_metadata(
                segment,
                camera_id="cam-001",
                start_time=start,
                end_time=start + timedelta(seconds=8, milliseconds=250),
            )

            self.assertEqual(target, metadata_path(segment))
            self.assertEqual(
                read_segment_metadata(segment, camera_id="cam-001"),
                {
                    "schema_version": 1,
                    "camera_id": "cam-001",
                    "start_time": "2026-11-11T01:00:00Z",
                    "end_time": "2026-11-11T01:00:08.250000Z",
                    "complete": True,
                },
            )

    def test_rejects_missing_incomplete_or_mismatched_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            segment = Path(temporary) / "recording.ts"
            self.assertIsNone(read_segment_metadata(segment, camera_id="cam-001"))
            sidecar = metadata_path(segment)
            sidecar.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "camera_id": "cam-001",
                        "start_time": "2026-11-11T01:00:00Z",
                        "end_time": "2026-11-11T01:00:08Z",
                        "complete": False,
                    }
                ),
                encoding="utf-8",
            )
            self.assertIsNone(read_segment_metadata(segment, camera_id="cam-001"))
            self.assertIsNone(read_segment_metadata(segment, camera_id="cam-002"))

    def test_rejects_invalid_time_range(self):
        with tempfile.TemporaryDirectory() as temporary:
            segment = Path(temporary) / "recording.ts"
            with self.assertRaisesRegex(ValueError, "after start"):
                write_segment_metadata(
                    segment,
                    camera_id="cam-001",
                    start_time=datetime(2026, 11, 11, tzinfo=UTC),
                    end_time=datetime(2026, 11, 11, tzinfo=UTC),
                )

    def test_uses_half_open_time_overlap(self):
        start = datetime(2026, 11, 11, 1, 0, tzinfo=UTC)
        end = start + timedelta(seconds=10)
        self.assertTrue(
            segment_overlaps(
                start,
                end,
                start + timedelta(seconds=9),
                end + timedelta(seconds=1),
            )
        )
        self.assertFalse(segment_overlaps(start, end, end, end + timedelta(seconds=1)))
        self.assertFalse(segment_overlaps(start, end, start - timedelta(seconds=1), start))


if __name__ == "__main__":
    unittest.main()
