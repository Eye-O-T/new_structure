"""오래된 이력을 작은 트랜잭션으로 정리하고 아직 필요한 작업·이미지는 보호한다."""

from __future__ import annotations

from typing import Any

from ai_cctv_core.time import parse_utc, utc_now

from ..connection import Database
from .job_state import update_event_job_state

RETENTION_BATCH_SIZE = 1000


class RetentionRepositoryMixin:
    database: Database

    def expire_abandoned_object_jobs(self, cutoff: str, now: str) -> int:
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT id,event_id,stage FROM object_jobs WHERE created_at<? AND "
                "(state='pending' OR (state='running' AND lease_until<=?)) "
                "ORDER BY id LIMIT ?",
                (cutoff, now, RETENTION_BATCH_SIZE),
            ).fetchall()
            connection.executemany(
                "UPDATE object_jobs SET state='failed',lease_id=NULL,lease_until=NULL,"
                "updated_at=? WHERE id=?",
                [(now, row["id"]) for row in rows],
            )
            for row in rows:
                update_event_job_state(
                    connection,
                    row["event_id"],
                    row["stage"],
                    "failed",
                    now,
                    error_code="OBJECT_RETENTION_EXPIRED",
                )
            return len(rows)

    def purge_expired_history(
        self,
        cutoff: str,
        now: str,
        *,
        protected_events: frozenset[tuple[str, str]],
        protected_paths: frozenset[str],
        purge_events: bool,
    ) -> dict[str, int]:
        counts: dict[str, int] = {}
        with self.database.transaction() as connection:
            connection.execute(
                "CREATE TEMP TABLE protected_events(camera_id TEXT,source_event_id TEXT,"
                "PRIMARY KEY(camera_id,source_event_id))"
            )
            connection.executemany(
                "INSERT INTO protected_events VALUES (?,?)", protected_events
            )
            connection.execute(
                "CREATE TEMP TABLE protected_paths(path TEXT PRIMARY KEY)"
            )
            connection.executemany(
                "INSERT INTO protected_paths VALUES (?)",
                [(p,) for p in protected_paths],
            )

            def remove(table: str, predicate: str, parameters: tuple[Any, ...]) -> None:
                result = connection.execute(
                    f"DELETE FROM {table} WHERE rowid IN (SELECT rowid FROM {table} "
                    f"WHERE {predicate} ORDER BY rowid LIMIT ?)",
                    (*parameters, RETENTION_BATCH_SIZE),
                )
                counts[table] = result.rowcount

            # 만료된 발송은 더 이상 전송하지 않으며, 유효한 sending 임대는 건드리지 않는다.
            connection.execute(
                "UPDATE push_deliveries SET state='cancelled',updated_at=? WHERE id IN "
                "(SELECT id FROM push_deliveries WHERE expires_at<=? AND "
                "(state='pending' OR (state='sending' AND lease_until<=?)) LIMIT ?)",
                (now, now, now, RETENTION_BATCH_SIZE),
            )
            remove(
                "push_deliveries",
                "state IN ('sent','failed','cancelled') AND updated_at<?",
                (cutoff,),
            )
            remove("revoked_tokens", "expires_at<=?", (now,))
            remove("refresh_tokens", "expires_at<?", (cutoff,))
            remove(
                "mobile_devices",
                "updated_at<? AND NOT EXISTS (SELECT 1 FROM refresh_tokens r "
                "WHERE r.user_id=mobile_devices.user_id AND COALESCE(r.family_id,r.jti)=mobile_devices.family_id "
                "AND r.expires_at>? AND r.revoked_at IS NULL AND r.replaced_by_jti IS NULL)",
                (cutoff, now),
            )
            remove(
                "recovery_jobs",
                "outage_ended_at<? AND updated_at<? AND "
                "(status='completed' OR (status='failed' AND attempt_count>=max_attempts))",
                (cutoff, cutoff),
            )
            remove(
                "edge_devices",
                "NOT EXISTS (SELECT 1 FROM cameras c WHERE c.edge_device_id=edge_devices.edge_device_id) "
                "AND NOT EXISTS (SELECT 1 FROM recovery_jobs j WHERE j.edge_device_id=edge_devices.edge_device_id) "
                "AND NOT EXISTS (SELECT 1 FROM events e WHERE json_extract(e.metadata_json,'$.recovery_edge_device_id')=edge_devices.edge_device_id)",
                (),
            )
            if purge_events:
                remove(
                    "events",
                    "occurred_at<? AND created_at<? "
                    "AND NOT EXISTS (SELECT 1 FROM object_jobs j WHERE j.event_id=events.id "
                    "AND (j.state IN ('pending','running') OR j.updated_at>=?)) "
                    "AND NOT EXISTS (SELECT 1 FROM push_deliveries p WHERE p.event_id=events.id "
                    "AND (p.state IN ('pending','sending') OR p.updated_at>=?)) "
                    "AND NOT EXISTS (SELECT 1 FROM protected_events p WHERE p.camera_id=events.camera_id "
                    "AND p.source_event_id=events.source_event_id) "
                    "AND NOT EXISTS (SELECT 1 FROM protected_paths p WHERE p.path=events.snapshot_path "
                    "OR p.path=json_extract(events.metadata_json,'$.object.crop_path') "
                    "OR p.path=json_extract(events.metadata_json,'$.object.annotated_snapshot_path'))",
                    (cutoff, cutoff, cutoff, cutoff),
                )
                remove(
                    "person_identity_links",
                    "created_at<? AND NOT EXISTS (SELECT 1 FROM events e "
                    "WHERE e.camera_id=person_identity_links.camera_id AND e.person_id=person_identity_links.person_id "
                    "AND json_extract(e.metadata_json,'$.tracking_session_id')=person_identity_links.tracking_session_id) "
                    "AND NOT EXISTS (SELECT 1 FROM person_track_presence p "
                    "WHERE p.camera_id=person_identity_links.camera_id AND p.person_id=person_identity_links.person_id "
                    "AND p.tracking_session_id=person_identity_links.tracking_session_id AND p.last_seen_at>=?)",
                    (cutoff, cutoff),
                )
                remove(
                    "person_track_presence",
                    "last_seen_at<? AND NOT EXISTS (SELECT 1 FROM events e "
                    "WHERE e.camera_id=person_track_presence.camera_id AND e.person_id=person_track_presence.person_id "
                    "AND json_extract(e.metadata_json,'$.tracking_session_id')=person_track_presence.tracking_session_id)",
                    (cutoff,),
                )
            # 삭제 완료 tombstone도 한 보관 기간 더 남겨 늦은 완료 통지의 중복 등록을 억제한다.
            remove("recording_segments", "status='deleted' AND updated_at<?", (cutoff,))
            remove("identity_gallery", "observed_at<?", (cutoff,))
        return counts

    def snapshot_is_referenced(self, path: str) -> bool:
        with self.database.connection() as connection:
            return (
                connection.execute(
                    "SELECT 1 FROM events WHERE snapshot_path=? "
                    "OR json_extract(metadata_json,'$.object.crop_path')=? "
                    "OR json_extract(metadata_json,'$.object.annotated_snapshot_path')=? LIMIT 1",
                    (path, path, path),
                ).fetchone()
                is not None
            )

    def queue_counts(self) -> dict[str, dict[str, int]]:
        with self.database.connection() as connection:
            queues = {
                stage: {
                    state: 0
                    for state in (
                        "pending",
                        "running",
                        "failed",
                        "unconfigured",
                        "complete",
                    )
                }
                for stage in ("identity", "analysis")
            }
            for row in connection.execute(
                "SELECT stage,state,COUNT(*) AS count FROM object_jobs GROUP BY stage,state"
            ):
                queues[row["stage"]][row["state"]] = row["count"]
            for queue, table, column, states in (
                (
                    "recovery",
                    "recovery_jobs",
                    "status",
                    (
                        "detected",
                        "waiting_for_recovery",
                        "downloading",
                        "indexing",
                        "completed",
                        "failed",
                    ),
                ),
                (
                    "push",
                    "push_deliveries",
                    "state",
                    ("pending", "sending", "sent", "failed", "cancelled"),
                ),
            ):
                queues[queue] = dict.fromkeys(states, 0)
                for row in connection.execute(
                    f"SELECT {column},COUNT(*) AS count FROM {table} GROUP BY {column}"
                ):
                    queues[queue][row[column]] = row["count"]
            return queues

    def queue_metrics(self) -> dict[str, dict[str, Any]]:
        now = utc_now()
        metrics = {}
        with self.database.connection() as connection:
            for queue, table, column, pending, success, condition, parameters in (
                (
                    "identity",
                    "object_jobs",
                    "state",
                    "'pending','running'",
                    "complete",
                    "stage=?",
                    ("identity",),
                ),
                (
                    "analysis",
                    "object_jobs",
                    "state",
                    "'pending','running'",
                    "complete",
                    "stage=?",
                    ("analysis",),
                ),
                (
                    "recovery",
                    "recovery_jobs",
                    "status",
                    "'detected','waiting_for_recovery','downloading','indexing'",
                    "completed",
                    "1",
                    (),
                ),
                (
                    "push",
                    "push_deliveries",
                    "state",
                    "'pending','sending'",
                    "sent",
                    "1",
                    (),
                ),
            ):
                row = connection.execute(
                    f"SELECT MIN(CASE WHEN {column} IN ({pending}) THEN created_at END) AS oldest,"
                    f"MAX(CASE WHEN {column}='{success}' THEN updated_at END) AS success,"
                    f"MAX(CASE WHEN {column}='failed' THEN updated_at END) AS failure "
                    f"FROM {table} WHERE {condition}",
                    parameters,
                ).fetchone()
                metrics[queue] = {
                    "oldest_pending_seconds": max(
                        0, round((now - parse_utc(row["oldest"])).total_seconds(), 3)
                    )
                    if row["oldest"]
                    else None,
                    "last_success_at": row["success"],
                    "last_failure_at": row["failure"],
                }
        return metrics
