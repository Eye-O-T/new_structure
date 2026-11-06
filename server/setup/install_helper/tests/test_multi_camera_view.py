import os
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5.QtWidgets import QApplication

from server.desktop.admin.application import CCTVMainWindow


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

    def test_event_sort_key_orders_occurrence_then_event_id(self):
        events = [
            {"id": 4, "occurred_at": "2026-11-04T05:00:00Z"},
            {"id": 2, "occurred_at": "2026-11-04T05:01:00Z"},
            {"id": 1, "occurred_at": "2026-11-04T05:00:00Z"},
        ]
        events.sort(key=self.window._event_sort_key)
        self.assertEqual([event["id"] for event in events], [1, 4, 2])

    def test_camera_change_rerenders_an_unchanged_event_list(self):
        event = {
            "id": 1,
            "type": "appear",
            "time": "2026-11-04T05:00:00Z",
            "occurred_at": "2026-11-04T05:00:00Z",
            "person_id": "7",
            "global_person_id": 42,
        }
        self.window._camera_events = {"cam-1": [event], "cam-2": [event]}
        self.window._selected_camera_id = "cam-1"
        self.window._render_selected_summary()
        self.window._selected_camera_id = "cam-2"
        self.window._render_selected_summary()
        self.app.processEvents()
        self.assertEqual(self.window._rendered_event_camera_id, "cam-2")
        self.assertEqual(self.window.event_list.count(), 1)

    def test_event_cards_append_oldest_first_and_keep_the_latest_30(self):
        for index in range(31):
            stamp = f"2026-11-04T00:00:{index:02d}Z"
            self.window.add_event(
                {
                    "id": index + 1,
                    "type": "appear",
                    "person_id": str(index + 1),
                    "global_person_id": None,
                    "occurred_at": stamp,
                    "time": stamp,
                }
            )
        self.assertEqual(self.window.event_list.count(), 30)
        visible_times = [
            self.window.event_list.itemAt(index)
            .widget()
            .layout()
            .itemAt(0)
            .widget()
            .text()
            for index in range(self.window.event_list.count())
        ]
        self.assertEqual(visible_times[0], "2026-11-04 09:00:01 KST")
        self.assertEqual(visible_times[-1], "2026-11-04 09:00:30 KST")

    def test_disabled_selected_camera_falls_back_to_first_available_camera(self):
        self.window.cameras[2]["enabled"] = False
        self.assertEqual(self.window._display_ids(), ["cam-1"])
        self.assertEqual(self.window._selected_camera_id, "cam-1")
