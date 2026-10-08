import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication

from server.setup.install_helper.desktop.application import CCTVMainWindow


class MultiCameraViewTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self):
        self.window = CCTVMainWindow(allow_insecure_http=True)
        self.window.cameras = [
            {"camera_id": f"cam-{index}", "name": f"Camera {index}", "enabled": True}
            for index in range(1, 6)
        ]
        self.window._selected_camera_id = "cam-3"

    def tearDown(self):
        self.window.close()
        self.window.deleteLater()

    def test_grid_starts_at_selected_camera_and_limits_to_four(self):
        self.window._view_mode = "grid"
        self.assertEqual(
            self.window._display_ids(), ["cam-3", "cam-4", "cam-5", "cam-1"]
        )

    def test_single_view_contains_only_selected_camera(self):
        self.window._view_mode = "single"
        self.assertEqual(self.window._display_ids(), ["cam-3"])

    def test_disabled_selected_camera_falls_back_to_first_available_camera(self):
        self.window.cameras[2]["enabled"] = False
        self.assertEqual(self.window._display_ids(), ["cam-1"])
        self.assertEqual(self.window._selected_camera_id, "cam-1")
