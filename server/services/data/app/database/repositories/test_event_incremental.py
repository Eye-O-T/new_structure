import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from ai_cctv_core.contracts.objects import (
    IdentityDescriptor,
    ObjectJobCompletion,
    ObjectObservation,
)

from ..connection import Database
from . import DataRepository
from .events import analysis_completed_source_event_id


def _sample_observation(session_id: str = "a" * 32) -> dict:
    return ObjectObservation(
        tracking_session_id=session_id,
        bbox=[10, 20, 110, 220],
        frame_width=1920,
        frame_height=1080,
        crop_path="crops/person.jpg",
    ).model_dump(mode="json")


class IncrementalEventTests(unittest.TestCase):
    def test_empty_object_queue_does_not_take_write_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = DataRepository(Database(Path(directory) / "data.sqlite"))
            repository.initialize()
            with patch.object(repository.database, "transaction", side_effect=AssertionError("write lock")):
                self.assertIsNone(repository.claim_object_job("analysis"))

    def test_concurrent_claims_assign_one_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = DataRepository(Database(Path(directory) / "data.sqlite"))
            repository.initialize()
            with repository.database.transaction() as connection:
                connection.execute(
                    "INSERT INTO cameras(camera_id,name,stream_path,created_at,updated_at) "
                    "VALUES('cam_001','Camera','cam_001','now','now')"
                )
            self._create_appearance(repository)
            with ThreadPoolExecutor(max_workers=2) as executor:
                claims = list(executor.map(repository.claim_object_job, ("analysis", "analysis")))
            self.assertEqual(sum(claim is not None for claim in claims), 1)

    def test_exhausted_pending_job_is_marked_failed_even_if_not_due(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = DataRepository(Database(Path(directory) / "data.sqlite"))
            repository.initialize()
            with repository.database.transaction() as connection:
                connection.execute(
                    "INSERT INTO cameras(camera_id,name,stream_path,created_at,updated_at) "
                    "VALUES('cam_001','Camera','cam_001','now','now')"
                )
            self._create_appearance(repository)
            with repository.database.transaction() as connection:
                connection.execute(
                    "UPDATE object_jobs SET attempts=5,next_attempt_at='9999-01-01T00:00:00Z' "
                    "WHERE stage='analysis'"
                )
            self.assertIsNone(repository.claim_object_job("analysis"))
            with repository.database.connection() as connection:
                row = connection.execute(
                    "SELECT state FROM object_jobs WHERE stage='analysis'"
                ).fetchone()
            self.assertEqual(row["state"], "failed")

    def test_late_and_sparse_event_ids_are_ordered_by_storage_id(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = DataRepository(Database(Path(directory) / "data.sqlite"))
            repository.initialize()
            with repository.database.transaction() as connection:
                connection.execute(
                    "INSERT INTO cameras(camera_id,name,stream_path,created_at,updated_at) "
                    "VALUES('cam_001','Camera','cam_001','now','now')"
                )
                for event_id, occurred_at in ((1, "2026-11-03T08:00:00Z"), (3, "2026-11-03T07:00:00Z")):
                    connection.execute(
                        "INSERT INTO events(id,camera_id,event_type,occurred_at,created_at) "
                        "VALUES(?, 'cam_001','person_appeared',?, 'now')",
                        (event_id, occurred_at),
                    )

            late = repository.search_events(
                camera_id="cam_001", event_type=None, start_time=None,
                end_time=None, limit=10, offset=0, snapshot_max_id=3,
                after_event_id=1,
            )
            self.assertEqual([int(item["id"]) for item in late], [3])
            self.assertEqual(late[0]["occurred_at"], "2026-11-03T07:00:00Z")

    def _create_appearance(
        self,
        repository: DataRepository,
        *,
        camera_id: str = "cam_001",
        person_id: str = "7",
        session_id: str = "a" * 32,
        occurred_at: str = "2026-11-06T06:00:00.000Z",
    ) -> dict:
        observation = _sample_observation(session_id)
        return repository.create_event(
            {
                "camera_id": camera_id,
                "event_type": "person_appeared",
                "occurred_at": occurred_at,
                "person_id": person_id,
                "confidence": 0.91,
                "object_observation": observation,
                "metadata": {
                    "object": observation,
                    "tracking_session_id": session_id,
                    "identity": {"status": "pending"},
                    "analysis": {"status": "pending"},
                },
            }
        )

    def test_analysis_completion_commits_event_atomically_without_jobs_push_or_recordings(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = DataRepository(Database(Path(directory) / "data.sqlite"))
            repository.initialize()
            with repository.database.transaction() as connection:
                connection.execute(
                    "INSERT INTO cameras(camera_id,name,stream_path,created_at,updated_at) "
                    "VALUES('cam_001','Camera','cam_001','now','now')"
                )
            appearance = self._create_appearance(repository)
            job = repository.claim_object_job("analysis")
            self.assertIsNotNone(job)
            result_payload = {
                "backend": "local_appearance",
                "clothing_colors": {
                    "upper": {
                        "dominant_color": {
                            "name": "blue",
                            "rgb": [20, 60, 180],
                            "confidence": 0.8123,
                        }
                    }
                },
            }
            accepted = repository.complete_object_job(
                "analysis",
                job["id"],
                ObjectJobCompletion(
                    lease_id=job["lease_id"],
                    outcome="complete",
                    metadata=result_payload,
                ),
            )
            self.assertTrue(accepted)

            # Duplicate submission with same lease is rejected without creating a second event
            duplicate = repository.complete_object_job(
                "analysis",
                job["id"],
                ObjectJobCompletion(
                    lease_id=job["lease_id"],
                    outcome="complete",
                    metadata=result_payload,
                ),
            )
            self.assertFalse(duplicate)

            completed_events = repository.search_events(
                camera_id="cam_001",
                event_type="analysis_completed",
                start_time=None,
                end_time=None,
                limit=10,
                offset=0,
            )
            self.assertEqual(len(completed_events), 1)
            completed = completed_events[0]
            self.assertEqual(completed["person_id"], "7")
            self.assertIsNone(completed["global_person_id"])
            self.assertEqual(
                completed["source_event_id"],
                analysis_completed_source_event_id(int(appearance["id"])),
            )
            self.assertEqual(
                completed["metadata"]["appearance_event_id"], int(appearance["id"])
            )
            self.assertEqual(
                completed["metadata"]["appearance_occurred_at"],
                appearance["occurred_at"],
            )
            self.assertEqual(completed["metadata"]["analysis_job_id"], int(job["id"]))
            self.assertEqual(completed["metadata"]["tracking_session_id"], "a" * 32)
            self.assertEqual(completed["metadata"]["analysis"]["status"], "complete")
            self.assertEqual(
                completed["metadata"]["analysis"]["result"], result_payload
            )
            self.assertEqual(completed["recording_segment_ids"], [])
            self.assertIsNone(completed["recording_segment_id"])

            # Verify no object_jobs or push_deliveries were created for the completion event
            with repository.database.connection() as connection:
                job_count = connection.execute(
                    "SELECT COUNT(*) FROM object_jobs WHERE event_id=?",
                    (completed["id"],),
                ).fetchone()[0]
                push_count = connection.execute(
                    "SELECT COUNT(*) FROM push_deliveries WHERE event_id=?",
                    (completed["id"],),
                ).fetchone()[0]
            self.assertEqual(job_count, 0)
            self.assertEqual(push_count, 0)

            # Verify recording segment linking skips analysis_completed events
            segment, _ = repository.create_segment(
                {
                    "camera_id": "cam_001",
                    "start_time": "2020-01-01T00:00:00.000Z",
                    "end_time": "2030-01-01T00:00:00.000Z",
                    "relative_path": "recordings/cam_001/seg1.mp4",
                    "format": "mp4",
                    "duration_ms": 10000,
                    "file_size": 1024,
                    "source": "central",
                    "status": "ready",
                },
                pre_roll_seconds=5,
                post_roll_seconds=5,
            )
            repository.repair_event_recording_links(5, 5)
            refreshed_completed = repository.get_event(int(completed["id"]))
            self.assertEqual(refreshed_completed["recording_segment_ids"], [])
            self.assertIsNone(refreshed_completed["recording_segment_id"])
            refreshed_appearance = repository.get_event(int(appearance["id"]))
            self.assertEqual(
                refreshed_appearance["recording_segment_ids"], [int(segment["id"])]
            )

    def test_event_insert_failure_rolls_back_job_and_metadata_until_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = DataRepository(Database(Path(directory) / "data.sqlite"))
            repository.initialize()
            with repository.database.transaction() as connection:
                connection.execute(
                    "INSERT INTO cameras(camera_id,name,stream_path,created_at,updated_at) "
                    "VALUES('cam_001','Camera','cam_001','now','now')"
                )
                connection.execute(
                    "CREATE TRIGGER fail_analysis_completed "
                    "BEFORE INSERT ON events "
                    "WHEN NEW.event_type = 'analysis_completed' "
                    "BEGIN SELECT RAISE(ABORT, 'simulated insert failure'); END;"
                )
            appearance = self._create_appearance(repository)
            job = repository.claim_object_job("analysis")
            completion = ObjectJobCompletion(
                lease_id=job["lease_id"],
                outcome="complete",
                metadata={"backend": "local_appearance"},
            )
            with self.assertRaises(sqlite3.DatabaseError):
                repository.complete_object_job("analysis", job["id"], completion)

            # Verify rollback: job is still running and no analysis_completed event exists
            current_appearance = repository.get_event(int(appearance["id"]))
            self.assertEqual(
                current_appearance["metadata"]["analysis"]["status"], "running"
            )
            with repository.database.transaction() as connection:
                connection.execute("DROP TRIGGER fail_analysis_completed")

            # Retry with the same lease succeeds and creates 1 completion event
            self.assertTrue(
                repository.complete_object_job("analysis", job["id"], completion)
            )
            completed_events = repository.search_events(
                camera_id="cam_001",
                event_type="analysis_completed",
                start_time=None,
                end_time=None,
                limit=10,
                offset=0,
            )
            self.assertEqual(len(completed_events), 1)

    def test_non_complete_outcomes_and_retries_only_emit_once_on_success(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = DataRepository(Database(Path(directory) / "data.sqlite"))
            repository.initialize()
            with repository.database.transaction() as connection:
                connection.execute(
                    "INSERT INTO cameras(camera_id,name,stream_path,created_at,updated_at) "
                    "VALUES('cam_001','Camera','cam_001','now','now')"
                )
            self._create_appearance(repository, person_id="1", session_id="b" * 32)
            job1 = repository.claim_object_job("analysis")
            # Wrong lease_id does not complete
            self.assertFalse(
                repository.complete_object_job(
                    "analysis",
                    job1["id"],
                    ObjectJobCompletion(
                        lease_id="0" * 32,
                        outcome="complete",
                        metadata={"backend": "local_appearance"},
                    ),
                )
            )
            # Retry outcome does not create analysis_completed
            self.assertTrue(
                repository.complete_object_job(
                    "analysis",
                    job1["id"],
                    ObjectJobCompletion(
                        lease_id=job1["lease_id"],
                        outcome="retry",
                        metadata={"error_code": "MODEL_TIMEOUT"},
                    ),
                )
            )
            self.assertEqual(
                repository.search_events(
                    camera_id="cam_001",
                    event_type="analysis_completed",
                    start_time=None,
                    end_time=None,
                    limit=10,
                    offset=0,
                ),
                [],
            )
            # Make next_attempt_at immediate so it can be claimed again
            with repository.database.transaction() as connection:
                connection.execute(
                    "UPDATE object_jobs SET next_attempt_at='2000-01-01T00:00:00.000Z' WHERE id=?",
                    (job1["id"],),
                )
            reclaimed = repository.claim_object_job("analysis")
            self.assertEqual(reclaimed["id"], job1["id"])
            self.assertTrue(
                repository.complete_object_job(
                    "analysis",
                    reclaimed["id"],
                    ObjectJobCompletion(
                        lease_id=reclaimed["lease_id"],
                        outcome="complete",
                        metadata={"backend": "local_appearance"},
                    ),
                )
            )
            self.assertEqual(
                len(
                    repository.search_events(
                        camera_id="cam_001",
                        event_type="analysis_completed",
                        start_time=None,
                        end_time=None,
                        limit=10,
                        offset=0,
                    )
                ),
                1,
            )

    def test_identity_before_and_after_analysis_populates_global_person_id(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = DataRepository(Database(Path(directory) / "data.sqlite"))
            repository.initialize()
            with repository.database.transaction() as connection:
                connection.execute(
                    "INSERT INTO cameras(camera_id,name,stream_path,created_at,updated_at) "
                    "VALUES('cam_001','Camera 1','cam_001','now','now'),"
                    "('cam_002','Camera 2','cam_002','now','now')"
                )
            # Case 1: Identity finishes BEFORE analysis on cam_001
            self._create_appearance(
                repository, camera_id="cam_001", person_id="7", session_id="c" * 32
            )
            id_job_1 = repository.claim_object_job("identity")
            an_job_1 = repository.claim_object_job("analysis")
            vector_1 = [1.0] + [0.0] * 15
            self.assertTrue(
                repository.complete_object_job(
                    "identity",
                    id_job_1["id"],
                    ObjectJobCompletion(
                        lease_id=id_job_1["lease_id"],
                        outcome="complete",
                        identity_descriptor=IdentityDescriptor(
                            space_id="osnet_x0_25", features=vector_1
                        ),
                        metadata={},
                    ),
                )
            )
            self.assertTrue(
                repository.complete_object_job(
                    "analysis",
                    an_job_1["id"],
                    ObjectJobCompletion(
                        lease_id=an_job_1["lease_id"],
                        outcome="complete",
                        metadata={"backend": "local_appearance"},
                    ),
                )
            )
            cam1_completed = repository.search_events(
                camera_id="cam_001",
                event_type="analysis_completed",
                start_time=None,
                end_time=None,
                limit=10,
                offset=0,
            )
            self.assertEqual(len(cam1_completed), 1)
            self.assertIsNotNone(cam1_completed[0]["global_person_id"])
            assigned_gpid = cam1_completed[0]["global_person_id"]

            # Case 2: Analysis finishes BEFORE identity on cam_002 (same pid "7", different camera/session)
            self._create_appearance(
                repository,
                camera_id="cam_002",
                person_id="7",
                session_id="d" * 32,
                occurred_at="2026-11-06T06:01:00.000Z",
            )
            id_job_2 = repository.claim_object_job("identity")
            an_job_2 = repository.claim_object_job("analysis")
            self.assertTrue(
                repository.complete_object_job(
                    "analysis",
                    an_job_2["id"],
                    ObjectJobCompletion(
                        lease_id=an_job_2["lease_id"],
                        outcome="complete",
                        metadata={"backend": "par_resnet34", "attributes": {"hat": {"value": "Hat", "score": 0.9}}},
                    ),
                )
            )
            cam2_completed_before = repository.search_events(
                camera_id="cam_002",
                event_type="analysis_completed",
                start_time=None,
                end_time=None,
                limit=10,
                offset=0,
            )
            self.assertEqual(len(cam2_completed_before), 1)
            self.assertIsNone(cam2_completed_before[0]["global_person_id"])

            # Now complete identity on cam_002 matching vector_1
            self.assertTrue(
                repository.complete_object_job(
                    "identity",
                    id_job_2["id"],
                    ObjectJobCompletion(
                        lease_id=id_job_2["lease_id"],
                        outcome="complete",
                        identity_descriptor=IdentityDescriptor(
                            space_id="osnet_x0_25", features=vector_1
                        ),
                        metadata={},
                    ),
                )
            )
            cam2_completed_after = repository.get_event(int(cam2_completed_before[0]["id"]))
            self.assertEqual(cam2_completed_after["global_person_id"], assigned_gpid)

    def test_backfill_and_retention_preserve_historical_and_expired_results(self):
        with tempfile.TemporaryDirectory() as directory:
            repository = DataRepository(Database(Path(directory) / "data.sqlite"))
            repository.initialize()
            with repository.database.transaction() as connection:
                connection.execute(
                    "INSERT INTO cameras(camera_id,name,stream_path,created_at,updated_at) "
                    "VALUES('cam_001','Camera','cam_001','now','now')"
                )
                # 1. Historical completed event whose object_jobs row was already cleaned up
                connection.execute(
                    "INSERT INTO events(id,camera_id,event_type,occurred_at,person_id,global_person_id,metadata_json,created_at) "
                    "VALUES(10,'cam_001','person_appeared','2026-09-01T00:00:00.000Z','7',42,?, '2026-09-01T00:00:00.000Z')",
                    (
                        json.dumps(
                            {
                                "tracking_session_id": "hist-1",
                                "analysis": {
                                    "status": "complete",
                                    "updated_at": "2026-11-05T01:02:03.000Z",
                                    "result": {"backend": "local_appearance"},
                                },
                            }
                        ),
                    ),
                )
                # 2. Historical event with missing updated_at -> should be skipped, not fabricated
                connection.execute(
                    "INSERT INTO events(id,camera_id,event_type,occurred_at,person_id,global_person_id,metadata_json,created_at) "
                    "VALUES(11,'cam_001','person_appeared','2026-09-01T00:01:00.000Z','8',NULL,?, '2026-09-01T00:01:00.000Z')",
                    (
                        json.dumps(
                            {
                                "tracking_session_id": "hist-2",
                                "analysis": {
                                    "status": "complete",
                                    "result": {"backend": "local_appearance"},
                                },
                            }
                        ),
                    ),
                )

            first_run = repository.backfill_analysis_completed_events(batch_size=1)
            self.assertEqual(first_run, {"created": 1, "skipped": 1})
            second_run = repository.backfill_analysis_completed_events(batch_size=1)
            self.assertEqual(second_run, {"created": 0, "skipped": 1})

            completed_list = repository.search_events(
                camera_id="cam_001",
                event_type="analysis_completed",
                start_time=None,
                end_time=None,
                limit=10,
                offset=0,
            )
            self.assertEqual(len(completed_list), 1)
            completed = completed_list[0]
            self.assertEqual(completed["occurred_at"], "2026-11-05T01:02:03.000Z")
            self.assertEqual(completed["person_id"], "7")
            self.assertEqual(completed["global_person_id"], 42)
            self.assertEqual(completed["metadata"]["appearance_event_id"], 10)
            self.assertNotIn("analysis_job_id", completed["metadata"])

            # Purge expired history before 2026-10-01: appearance event 10 is deleted,
            # but analysis_completed event from 2026-11-05 remains queryable with full result!
            repository.purge_expired_history(
                "2026-10-01T00:00:00.000Z",
                "2026-11-06T00:00:00.000Z",
                protected_events=frozenset(),
                protected_paths=frozenset(),
                purge_events=True,
            )
            self.assertIsNone(repository.get_event(10))
            surviving = repository.get_event(int(completed["id"]))
            self.assertIsNotNone(surviving)
            self.assertEqual(
                surviving["metadata"]["analysis"]["result"],
                {"backend": "local_appearance"},
            )


if __name__ == "__main__":
    unittest.main()
