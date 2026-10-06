import unittest

from server.setup.install_helper.desktop.overlay import overlay_items, scale_bbox_to_widget


class OverlayGeometryTests(unittest.TestCase):
    def test_same_aspect_ratio_scales_coordinates(self):
        self.assertEqual(
            scale_bbox_to_widget((100, 100, 500, 700), 1920, 1080, 960, 540),
            (50.0, 50.0, 250.0, 350.0),
        )

    def test_letterbox_adds_vertical_offset(self):
        rect = scale_bbox_to_widget((0, 0, 1920, 1080), 1920, 1080, 1000, 1000)
        self.assertAlmostEqual(rect[1], 218.75)
        self.assertAlmostEqual(rect[3], 781.25)

    def test_pillarbox_adds_horizontal_offset(self):
        rect = scale_bbox_to_widget((0, 0, 1080, 1920), 1080, 1920, 1000, 1000)
        self.assertAlmostEqual(rect[0], 218.75)
        self.assertAlmostEqual(rect[2], 781.25)

    def test_stale_and_invalid_boxes_are_not_rendered(self):
        self.assertEqual(overlay_items({"stale": True, "objects": []}, 1000, 1000), [])
        self.assertEqual(
            overlay_items(
                {"frame_width": 1920, "frame_height": 1080, "objects": [{"bbox": [1, 2]}]},
                1000,
                1000,
            ),
            [],
        )


if __name__ == "__main__":
    unittest.main()
