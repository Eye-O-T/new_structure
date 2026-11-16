from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

_CACHE_PATH = Path(__file__).parent / "app" / "storage" / "recording_cache.py"
_CACHE_SPEC = importlib.util.spec_from_file_location(
    "recording_cache_under_test", _CACHE_PATH
)
assert _CACHE_SPEC is not None and _CACHE_SPEC.loader is not None
recording_cache = importlib.util.module_from_spec(_CACHE_SPEC)
_CACHE_SPEC.loader.exec_module(recording_cache)


class RecordingFileCacheTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        recording_cache.invalidate_active_recording_file_cache()

    def tearDown(self) -> None:
        recording_cache.invalidate_active_recording_file_cache()
        self.temp.cleanup()

    def test_reuses_result_until_ttl_expires(self) -> None:
        inspect = Mock(side_effect=[True, False])
        with patch.object(
            recording_cache.time, "monotonic", side_effect=[10.0, 10.0, 12.1, 12.1]
        ):
            self.assertTrue(
                recording_cache.active_recording_file_cached(
                    "cam-1", self.root, inspect
                )
            )
            self.assertTrue(
                recording_cache.active_recording_file_cached(
                    "cam-1", self.root, inspect
                )
            )
            self.assertFalse(
                recording_cache.active_recording_file_cached(
                    "cam-1", self.root, inspect
                )
            )
        self.assertEqual(inspect.call_count, 2)

    def test_camera_invalidation_refreshes_cached_state(self) -> None:
        inspect = Mock(side_effect=[False, True])
        self.assertFalse(
            recording_cache.active_recording_file_cached("cam-1", self.root, inspect)
        )
        recording_cache.invalidate_active_recording_file_cache("cam-1")
        self.assertTrue(
            recording_cache.active_recording_file_cached(
                "cam-1", self.root, inspect
            )
        )
        self.assertEqual(inspect.call_count, 2)

    def test_caches_are_isolated_by_storage_root(self) -> None:
        other_root = self.root / "other"
        other_root.mkdir()
        first = Mock(return_value=True)
        second = Mock(return_value=False)
        self.assertTrue(
            recording_cache.active_recording_file_cached("cam-1", self.root, first)
        )
        self.assertFalse(
            recording_cache.active_recording_file_cached(
                "cam-1", other_root, second
            )
        )
        self.assertEqual(first.call_count, 1)
        self.assertEqual(second.call_count, 1)


if __name__ == "__main__":
    unittest.main()
