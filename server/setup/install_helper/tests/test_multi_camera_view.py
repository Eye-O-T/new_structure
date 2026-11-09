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

    def test_analysis_completed_card_format_and_excludes_from_appear_disappear_counts(self):
        self.window.add_event(
            {
                "id": 1,
                "type": "appear",
                "person_id": "7",
                "global_person_id": None,
                "occurred_at": "2026-11-06T06:00:00Z",
                "time": "2026-11-06T06:00:00Z",
            }
        )
        self.window.add_event(
            {
                "id": 2,
                "type": "analysis_completed",
                "event_type": "analysis_completed",
                "person_id": "7",
                "global_person_id": None,
                "occurred_at": "2026-11-06T06:00:02Z",
                "time": "2026-11-06T06:00:02Z",
            }
        )
        self.window.add_event(
            {
                "id": 3,
                "type": "analysis_completed",
                "event_type": "analysis_completed",
                "person_id": "7",
                "global_person_id": 42,
                "occurred_at": "2026-11-06T06:00:03Z",
                "time": "2026-11-06T06:00:03Z",
                "metadata": {
                    "analysis": {
                        "status": "complete",
                        "result": {
                            "backend": "par_resnet34",
                            "attributes": {
                                "gender": {"value": "Male", "score": 0.71},
                                "age": {"value": "16-30", "score": 0.52},
                            },
                        },
                    }
                },
            }
        )
        self.window.add_event(
            {
                "id": 4,
                "type": "disappear",
                "person_id": "7",
                "global_person_id": 42,
                "occurred_at": "2026-11-06T06:00:05Z",
                "time": "2026-11-06T06:00:05Z",
            }
        )
        self.assertEqual(self.window.appear_count, 1)
        self.assertEqual(self.window.disappear_count, 1)
        self.assertEqual(self.window.metric_appear["value"].text(), "1")
        self.assertEqual(self.window.metric_disappear["value"].text(), "1")
        descriptions = [
            self.window.event_list.itemAt(index)
            .widget()
            .layout()
            .itemAt(1)
            .widget()
            .text()
            for index in range(self.window.event_list.count())
        ]
        self.assertEqual(
            descriptions,
            [
                "pid:7 gpid:null 출현",
                "pid:7/gpid:null 분석 완료",
                "pid:7/gpid:42 분석 완료 (성별: Male, 연령: 16-30)",
                "pid:7 gpid:42 사라짐",
            ],
        )

    def test_on_event_updates_existing_event_gpid_without_duplicate_card(self):
        worker = type("DummyWorker", (), {"running": True, "stop": lambda self: None})()
        self.window._selected_camera_id = "cam-1"
        self.window._workers["cam-1"] = worker
        self.window._camera_recordings["cam-1"] = {
            "camera_id": "cam-1",
            "enabled": True,
            "recording_enabled": True,
            "recording_state": "recording",
        }
        self.window._on_event(
            "cam-1",
            worker,
            {
                "id": 50,
                "type": "analysis_completed",
                "event_type": "analysis_completed",
                "person_id": "7",
                "global_person_id": None,
                "occurred_at": "2026-11-06T06:00:03Z",
                "time": "2026-11-06T06:00:03Z",
            },
        )
        self.assertEqual(self.window.event_list.count(), 1)
        self.assertEqual(
            self.window.event_list.itemAt(0).widget().layout().itemAt(1).widget().text(),
            "pid:7/gpid:null 분석 완료",
        )

        # Same event ID arrives with resolved global_person_id=42
        self.window._on_event(
            "cam-1",
            worker,
            {
                "id": 50,
                "type": "analysis_completed",
                "event_type": "analysis_completed",
                "person_id": "7",
                "global_person_id": 42,
                "occurred_at": "2026-11-06T06:00:03Z",
                "time": "2026-11-06T06:00:03Z",
            },
        )
        self.assertEqual(self.window.event_list.count(), 1)
        self.assertEqual(
            self.window.event_list.itemAt(0).widget().layout().itemAt(1).widget().text(),
            "pid:7/gpid:42 분석 완료",
        )
