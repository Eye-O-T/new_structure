import unittest

from server.services.external.app.api.events import _public_event


class PublicEventTests(unittest.TestCase):
    def test_public_event_removes_all_storage_references(self):
        result = _public_event(
            {
                "id": "event-1",
                "camera_id": "cam-1",
                "snapshot_path": "events/snapshot.jpg",
                "metadata": {
                    "object": {
                        "crop_path": "events/crop.jpg",
                        "annotated_snapshot_path": "events/annotated.png",
                        "label": "person",
                    }
                },
            }
        )

        self.assertEqual(
            result["media"],
            {"snapshot": True, "crop": True, "annotated_snapshot": True},
        )
        self.assertNotIn("snapshot_path", result)
        self.assertNotIn("crop_path", result["metadata"]["object"])
        self.assertNotIn("annotated_snapshot_path", result["metadata"]["object"])

    def test_malformed_metadata_never_leaks_or_raises(self):
        values = (
            None,
            "invalid",
            [],
            {},
            {"object": None},
            {"object": "invalid"},
            {"object": []},
            {"object": {}},
        )
        for metadata in values:
            with self.subTest(metadata=metadata):
                result = _public_event(
                    {
                        "id": "event-1",
                        "camera_id": "cam-1",
                        "snapshot_path": "private/snapshot.jpg",
                        "metadata": metadata,
                    }
                )
                self.assertNotIn("snapshot_path", result)
                self.assertEqual(result["media"], {
                    "snapshot": True,
                    "crop": False,
                    "annotated_snapshot": False,
                })


if __name__ == "__main__":
    unittest.main()
