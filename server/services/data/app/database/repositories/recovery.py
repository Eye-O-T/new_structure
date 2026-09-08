# 연결 끊김·복구 이벤트를 복구 시간 구간으로 묶고 작업의 진행 상태와 재시도를 기록한다.
"""Recovery persistence and SQL operations."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

from ai_cctv_core.time import format_utc, parse_utc

from ..connection import Database
from .base import RECOVERY_EVENT_CORRELATION_SECONDS, _as_dict, _now


class RecoveryRepositoryMixin:
    database: Database

    def note_recovery_event(
        self,
        *,
        camera_id: str,
        event_type: str,
        occurred_at: str,
        max_attempts: int,
        settle_seconds: int = 15,
    ) -> dict[str, Any] | None:
        """Merge authoritative Edge outage reports into one recovery interval.

        Duplicate and reordered Edge reports for the same camera are correlated
        when a lost timestamp overlaps the stored interval (with up to 60 seconds
        of start-boundary skew), or a restored timestamp is within 60 seconds of
        the stored end boundary. Correlated boundaries use min(start)/max(end).
        A restore event received before its lost event is paired from the event
        journal. Expanding a claimed/completed interval increments ``revision``
        and requeues it, so an in-flight worker cannot complete stale bounds.
        Closed bounds become claimable only after the configured settle period,
        allowing the Edge splitmux writer to rotate its final active segment.

        Legacy ``network_failure``/``network_recovery`` events remain valid event
        history, but they describe the old inference-consumer signal and are not
        authoritative Edge publisher boundaries for segment recovery.
        """
        lost_types = {"central_connection_lost"}
        restored_types = {"central_connection_restored"}
        if event_type not in lost_types | restored_types:
            return None
        now = _now()

        def recovery_ready_at(outage_end: str) -> str:
            settled = format_utc(
                parse_utc(outage_end) + timedelta(seconds=settle_seconds)
            )
            return max(now, settled)

        def merge_bounds(
            connection: sqlite3.Connection,
            job: sqlite3.Row,
            *,
            start: str | None = None,
            end: str | None = None,
        ) -> sqlite3.Row:
            stored_start = str(job["outage_started_at"])
            merged_start = min(stored_start, start or stored_start)
            current_end = job["outage_ended_at"]
            merged_end = current_end
            if end is not None:
                merged_end = end if current_end is None else max(str(current_end), end)
            if merged_start == job["outage_started_at"] and merged_end == current_end:
                return job
            closed = merged_end is not None
            reset_attempts = job["status"] == "completed" or int(
                job["attempt_count"]
            ) >= int(job["max_attempts"])
            connection.execute(
                """
                UPDATE recovery_jobs
                SET outage_started_at = ?, outage_ended_at = ?,
                    status = ?,
                    attempt_count = CASE WHEN ? THEN 0 ELSE attempt_count END,
                    next_retry_at = ?, last_error = NULL,
                    revision = revision + 1, updated_at = ?
                WHERE id = ?
                """,
                (
                    merged_start,
                    merged_end,
                    "waiting_for_recovery" if closed else "detected",
                    int(reset_attempts),
                    recovery_ready_at(str(merged_end)) if closed else None,
                    now,
                    job["id"],
                ),
            )
            return connection.execute(
                "SELECT * FROM recovery_jobs WHERE id = ?", (job["id"],)
            ).fetchone()

        with self.database.transaction() as connection:
            if event_type in lost_types:
                open_job = connection.execute(
                    """
                    SELECT * FROM recovery_jobs
                    WHERE camera_id = ? AND outage_ended_at IS NULL
                    ORDER BY id DESC LIMIT 1
                    """,
                    (camera_id,),
                ).fetchone()
                dedup_upper = format_utc(
                    parse_utc(occurred_at)
                    + timedelta(seconds=RECOVERY_EVENT_CORRELATION_SECONDS)
                )
                existing = connection.execute(
                    """
                    SELECT * FROM recovery_jobs
                    WHERE camera_id = ?
                      AND outage_started_at <= ?
                      AND outage_ended_at IS NOT NULL
                      AND outage_ended_at >= ?
                    ORDER BY id DESC LIMIT 1
                    """,
                    (camera_id, dedup_upper, occurred_at),
                ).fetchone()
                if existing is not None:
                    return _as_dict(
                        merge_bounds(connection, existing, start=occurred_at)
                    )
                if open_job is not None:
                    return _as_dict(
                        merge_bounds(connection, open_job, start=occurred_at)
                    )

                # Event insertion precedes this call. If transport ordering
                # delivered a restore first, pair the earliest later restore
                # rather than leaving a permanently open job.
                pending_restore = connection.execute(
                    """
                    SELECT occurred_at FROM events
                    WHERE camera_id = ?
                      AND event_type = 'central_connection_restored'
                      AND occurred_at > ?
                    ORDER BY occurred_at, id LIMIT 1
                    """,
                    (camera_id, occurred_at),
                ).fetchone()
                outage_end = (
                    str(pending_restore["occurred_at"])
                    if pending_restore is not None
                    else None
                )
                cursor = connection.execute(
                    """
                    INSERT INTO recovery_jobs(
                        camera_id, outage_started_at, outage_ended_at, status,
                        max_attempts, next_retry_at, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        camera_id,
                        occurred_at,
                        outage_end,
                        "waiting_for_recovery" if outage_end else "detected",
                        max_attempts,
                        recovery_ready_at(outage_end) if outage_end else None,
                        now,
                        now,
                    ),
                )
                existing = connection.execute(
                    "SELECT * FROM recovery_jobs WHERE id = ?",
                    (cursor.lastrowid,),
                ).fetchone()
                return _as_dict(existing)

            job = connection.execute(
                """
                SELECT * FROM recovery_jobs
                WHERE camera_id = ? AND outage_ended_at IS NULL
                  AND outage_started_at < ?
                ORDER BY outage_started_at DESC, id DESC LIMIT 1
                """,
                (camera_id, occurred_at),
            ).fetchone()
            if job is not None:
                return _as_dict(merge_bounds(connection, job, end=occurred_at))

            correlation_start = format_utc(
                parse_utc(occurred_at)
                - timedelta(seconds=RECOVERY_EVENT_CORRELATION_SECONDS)
            )
            correlation_end = format_utc(
                parse_utc(occurred_at)
                + timedelta(seconds=RECOVERY_EVENT_CORRELATION_SECONDS)
            )
            job = connection.execute(
                """
                SELECT * FROM recovery_jobs
                WHERE camera_id = ? AND outage_ended_at IS NOT NULL
                  AND outage_started_at < ?
                  AND outage_ended_at BETWEEN ? AND ?
                ORDER BY outage_ended_at DESC, id DESC LIMIT 1
                """,
                (camera_id, occurred_at, correlation_start, correlation_end),
            ).fetchone()
            if job is None:
                return None
            return _as_dict(merge_bounds(connection, job, end=occurred_at))

    def claim_due_recovery_job(self) -> dict[str, Any] | None:
        now = _now()
        with self.database.transaction() as connection:
            row = connection.execute(
                """
                SELECT j.*, e.management_url, e.recovery_url, e.auth_token
                FROM recovery_jobs j
                JOIN cameras c ON c.camera_id = j.camera_id
                LEFT JOIN edge_devices e ON e.edge_device_id = c.edge_device_id
                WHERE j.outage_ended_at IS NOT NULL
                  AND j.attempt_count < j.max_attempts
                  AND j.status IN ('waiting_for_recovery', 'failed')
                  AND (j.next_retry_at IS NULL OR j.next_retry_at <= ?)
                ORDER BY COALESCE(j.next_retry_at, j.created_at), j.id
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                return None
            connection.execute(
                """
                UPDATE recovery_jobs
                SET status = 'downloading', attempt_count = attempt_count + 1,
                    last_error = NULL, updated_at = ?
                WHERE id = ?
                """,
                (now, row["id"]),
            )
            claimed = dict(row)
            claimed["status"] = "downloading"
            claimed["attempt_count"] = int(row["attempt_count"]) + 1
            claimed["updated_at"] = now
            return claimed

    def requeue_interrupted_recovery_jobs(self) -> int:
        """Make jobs leased by a terminated Data process retryable again."""

        now = _now()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                UPDATE recovery_jobs
                SET status = 'failed',
                    last_error = 'RECOVERY_INTERRUPTED',
                    next_retry_at = CASE
                        WHEN attempt_count < max_attempts THEN ?
                        ELSE NULL
                    END,
                    updated_at = ?
                WHERE status IN ('downloading', 'indexing')
                """,
                (now, now),
            )
            return int(cursor.rowcount)

    def update_recovery_job(
        self,
        job_id: int,
        *,
        status: str,
        last_error: str | None = None,
        next_retry_at: str | None = None,
        recovery_summary: dict[str, Any] | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any] | None:
        now = _now()
        summary_json = (
            json.dumps(recovery_summary, separators=(",", ":"))
            if recovery_summary is not None
            else None
        )
        with self.database.transaction() as connection:
            revision_clause = (
                # 복구 구간이 늘어나면 revision도 바뀐다. 예전 구간의 완료 보고로 덮어쓰지 않는다.
                " AND revision = ?" if expected_revision is not None else ""
            )
            parameters: tuple[Any, ...] = (
                status,
                last_error,
                next_retry_at,
                summary_json,
                now,
                job_id,
            )
            if expected_revision is not None:
                parameters = (*parameters, expected_revision)
            cursor = connection.execute(
                f"""
                UPDATE recovery_jobs
                SET status = ?, last_error = ?, next_retry_at = ?,
                    recovery_summary_json = COALESCE(?, recovery_summary_json),
                    updated_at = ?
                WHERE id = ?{revision_clause}
                """,
                parameters,
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                "SELECT * FROM recovery_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        result = _as_dict(row)
        if result is not None:
            raw_summary = result.pop("recovery_summary_json", None)
            if raw_summary:
                result["recovery_summary"] = json.loads(raw_summary)
        return result

    def get_recovery_job(self, job_id: int) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM recovery_jobs WHERE id = ?", (job_id,)
            ).fetchone()
        result = _as_dict(row)
        if result is not None and result.get("recovery_summary_json"):
            result["recovery_summary"] = json.loads(result.pop("recovery_summary_json"))
        return result

    def list_recovery_jobs(
        self, camera_id: str | None, limit: int, offset: int
    ) -> list[dict[str, Any]]:
        where = " WHERE camera_id = ?" if camera_id is not None else ""
        parameters: tuple[Any, ...] = (camera_id,) if camera_id is not None else ()
        with self.database.connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM recovery_jobs{where} "
                "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            raw_summary = item.pop("recovery_summary_json", None)
            if raw_summary:
                item["recovery_summary"] = json.loads(raw_summary)
            results.append(item)
        return results
