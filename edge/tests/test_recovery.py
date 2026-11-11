import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ai_cctv_edge.recovery import _recoverable_segments
from ai_cctv_edge.segments import write_segment_metadata


class RecoveryMetadataTests(unittest.TestCase):
    def test_only_completed_metadata_segments_are_recoverable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            complete = root / "complete.ts"
            complete.write_bytes(b"transport-stream")
            start = datetime(2026, 11, 11, 1, 0, tzinfo=UTC)
            write_segment_metadata(
                complete,
                camera_id="cam-001",
                start_time=start,
                end_time=start + timedelta(seconds=7, milliseconds=400),
            )
            legacy = root / "legacy.ts"
            legacy.write_bytes(b"old segment without exact timing")
            wrong_camera = root / "wrong-camera.ts"
            wrong_camera.write_bytes(b"other camera")
            write_segment_metadata(
                wrong_camera,
                camera_id="cam-002",
                start_time=start,
                end_time=start + timedelta(seconds=8),
            )

            self.assertEqual(
                _recoverable_segments(root, camera_id="cam-001"),
                (complete,),
            )


if __name__ == "__main__":
    unittest.main()
