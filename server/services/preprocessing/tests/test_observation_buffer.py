import unittest

import numpy as np

from ai_cctv_core.processing.observation_quality import evaluate_border_margin
from ..app.observation_buffer import quality


class ObservationQualityTests(unittest.TestCase):
    def test_border_margin_uses_float_thresholds_and_fixed_edge_order(self):
        result = evaluate_border_margin((38, 32, 1882, 1048), 1920, 1080)
        self.assertFalse(result["border_clear"])
        self.assertEqual(result["near_edges"], ["left", "top", "right", "bottom"])
        self.assertEqual(result["distances_px"], {"left": 38, "top": 32, "right": 38, "bottom": 32})

    def test_exact_integer_clearance_passes(self):
        result = evaluate_border_margin((39, 33, 1881, 1047), 1920, 1080)
        self.assertTrue(result["border_clear"])
        self.assertEqual(result["near_edges"], [])

    def test_valid_center_candidate_outranks_better_border_candidate(self):
        frame = np.random.default_rng(1).integers(0, 255, (480, 640, 3), dtype=np.uint8)
        center = quality(frame, {"bbox": (100, 50, 500, 430)})
        edge = quality(frame, {"bbox": (0, 40, 400, 430)})
        self.assertEqual(center[0:2], (1, 1))
        self.assertEqual(edge[0:2], (1, 0))
        self.assertGreater(center, edge)


if __name__ == "__main__":
    unittest.main()
