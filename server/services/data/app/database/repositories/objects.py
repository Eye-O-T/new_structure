"""Durable independent identity/analysis jobs; only Data owns SQLite."""

import json
import uuid
from datetime import timedelta

from ai_cctv_core.contracts.objects import ObjectJobCompletion
from ai_cctv_core.time import format_utc, utc_now


class ObjectRepositoryMixin:
    def requeue_unconfigured_objects(self, stage):
        now = format_utc(utc_now())
        with self.database.transaction() as connection:
            result = connection.execute(
                "UPDATE object_jobs SET state='pending',attempts=0,next_attempt_at=?,updated_at=? WHERE id IN (SELECT id FROM object_jobs WHERE stage=? AND state='unconfigured' ORDER BY id LIMIT 100)",
                (now, now, stage),
            )
            return result.rowcount

    def put_live_objects(self, camera_id, payload):
        stamp = format_utc(utc_now())
        observed = format_utc(payload.observed_at)
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO live_objects VALUES (?,?,?,?) ON CONFLICT(camera_id) DO UPDATE SET payload_json=excluded.payload_json,observed_at=excluded.observed_at,received_at=excluded.received_at WHERE excluded.observed_at>live_objects.observed_at",
                (camera_id, payload.model_dump_json(), observed, stamp),
            )

    def get_live_objects(self, camera_id):
        from ai_cctv_core.time import parse_utc

        now = utc_now()
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT l.* FROM live_objects l JOIN cameras c ON c.camera_id=l.camera_id WHERE l.camera_id=? AND c.enabled=1",
                (camera_id,),
            ).fetchone()
            if (
                row is None
                or not 0 <= (now - parse_utc(row["observed_at"])).total_seconds() <= 3
            ):
                return {"camera_id": camera_id, "objects": [], "stale": True}
            result = json.loads(row["payload_json"])
            links = {
                r["person_id"]: r["global_person_id"]
                for r in connection.execute(
                    "SELECT person_id,global_person_id FROM person_identity_links WHERE camera_id=? AND tracking_session_id=?",
                    (camera_id, result["tracking_session_id"]),
                ).fetchall()
            }
            for obj in result["objects"]:
                obj["global_person_id"] = links.get(obj["person_id"])
            return {**result, "camera_id": camera_id, "stale": False}

    def _enqueue_object_jobs(self, connection, event_id, values):
        observation = values.get("object_observation")
        if observation is None:
            return
        now = format_utc(utc_now())
        for stage in ("identity", "analysis"):
            connection.execute(
                "INSERT INTO object_jobs(event_id,stage,next_attempt_at,created_at,updated_at) VALUES (?,?,?,?,?)",
                (event_id, stage, now, now, now),
            )

    def claim_object_job(self, stage):
        now = utc_now()
        stamp = format_utc(now)
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE object_jobs SET state='failed',updated_at=? WHERE stage=? AND attempts>=5 AND (state='pending' OR (state='running' AND lease_until<=?))",
                (stamp, stage, stamp),
            )
            row = connection.execute(
                "SELECT j.id,j.event_id,j.attempts,e.camera_id,e.person_id,e.global_person_id,e.occurred_at,e.metadata_json FROM object_jobs j JOIN events e ON e.id=j.event_id WHERE j.stage=? AND j.attempts<5 AND ((j.state='pending' AND j.next_attempt_at<=?) OR (j.state='running' AND j.lease_until<=?)) ORDER BY j.id LIMIT 1",
                (stage, stamp, stamp),
            ).fetchone()
            if row is None:
                return None
            lease = uuid.uuid4().hex
            connection.execute(
                "UPDATE object_jobs SET state='running',attempts=attempts+1,lease_id=?,lease_until=?,updated_at=? WHERE id=?",
                (lease, format_utc(now + timedelta(minutes=5)), stamp, row["id"]),
            )
            result = dict(row)
            result["object_observation"] = json.loads(result.pop("metadata_json"))[
                "object"
            ]
            result["lease_id"] = lease
            return result

    def complete_object_job(self, stage, job_id, completion: ObjectJobCompletion):
        now = utc_now()
        stamp = format_utc(now)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT j.*,e.camera_id,e.person_id,e.metadata_json FROM object_jobs j JOIN events e ON e.id=j.event_id WHERE j.id=? AND j.stage=? AND j.state='running' AND j.lease_id=? AND j.lease_until>?",
                (job_id, stage, completion.lease_id, stamp),
            ).fetchone()
            if row is None:
                return False
            if stage != "identity" and completion.global_person_id is not None:
                raise ValueError("Only identity jobs may assign a global person ID")
            state = completion.outcome
            if state == "retry":
                state = "pending" if row["attempts"] < 5 else "failed"
            metadata = json.loads(row["metadata_json"])
            metadata[stage] = {
                "status": state,
                "updated_at": stamp,
                "result": completion.metadata,
            }
            if stage == "identity" and completion.global_person_id is not None:
                session = metadata["object"]["tracking_session_id"]
                existing = connection.execute(
                    "SELECT global_person_id FROM person_identity_links WHERE camera_id=? AND tracking_session_id=? AND person_id=?",
                    (row["camera_id"], session, row["person_id"]),
                ).fetchone()
                if (
                    existing
                    and existing["global_person_id"] != completion.global_person_id
                ):
                    raise ValueError("Identity already assigned for this camera track")
                connection.execute(
                    "INSERT OR IGNORE INTO person_identity_links VALUES (?,?,?,?,?)",
                    (
                        row["camera_id"],
                        session,
                        row["person_id"],
                        completion.global_person_id,
                        stamp,
                    ),
                )
                connection.execute(
                    "UPDATE events SET global_person_id=? WHERE camera_id=? AND person_id=? AND json_extract(metadata_json,'$.tracking_session_id')=?",
                    (
                        completion.global_person_id,
                        row["camera_id"],
                        row["person_id"],
                        session,
                    ),
                )
            connection.execute(
                "UPDATE events SET metadata_json=? WHERE id=?",
                (json.dumps(metadata, allow_nan=False), row["event_id"]),
            )
            connection.execute(
                "UPDATE object_jobs SET state=?,next_attempt_at=?,lease_id=NULL,lease_until=NULL,updated_at=? WHERE id=?",
                (
                    state,
                    format_utc(
                        now + timedelta(seconds=min(300, 15 * 2 ** row["attempts"]))
                    ),
                    stamp,
                    job_id,
                ),
            )
            return True
