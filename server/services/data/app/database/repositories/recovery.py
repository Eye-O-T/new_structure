# 연결 끊김·복구 이벤트를 복구 시간 구간으로 묶고 작업의 진행 상태와 재시도를 기록한다.

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
        """Edge의 중앙 연결 끊김·복구 보고를 하나의 복구 구간으로 합친다.

        끊김 시각이 기존 구간과 겹치거나(시작 경계 오차 60초 허용),
        복구 시각이 기존 끝 경계의 60초 이내이면 같은 구간으로 본다.
        시작은 최솟값, 끝은 최댓값을 쓰며 복구 보고가 먼저 오면 이벤트 일지에서 짝을 찾는다.
        임대·완료된 구간이 늘어나면 revision을 올려 재등록하고 이전 범위의 완료를 거절한다.
        끝이 정해진 구간도 설정된 안정화 시간이 지난 뒤 임대해 마지막 녹화 파일의 회전을 기다린다.

        구형 network_failure/network_recovery는 이력으로만 보존한다.
        추론 소비자의 신호이므로 Edge 송출 복구 구간의 근거로 사용하지 않는다."""
        lost_types = {"central_connection_lost"}
        restored_types = {"central_connection_restored"}
        if event_type not in lost_types | restored_types:
            return None
        now = _now()

        # 마지막 Edge 파일이 닫힐 시간을 확보하되 이미 지난 시각에는 즉시 재시도하도록 한다.
        def recovery_ready_at(outage_end: str) -> str:
            settled = format_utc(
                parse_utc(outage_end) + timedelta(seconds=settle_seconds)
            )
            return max(now, settled)

        # 기존 구간을 확장할 때 revision을 올리고 완료·시도 초과 작업에도 새 범위의 재시도를 허용한다.
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

                # 복구 보고가 먼저 저장됐다면 끊김 이후의 가장 이른 복구 보고와 짝을 지어 구간을 닫는다.
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

    # 복구 종료 시각과 재시도 조건이 갖춰진 작업 하나를 다운로드 상태로 원자적으로 전환한다.
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
        """종료된 Data 프로세스의 작업을 다시 시도할 수 있게 한다."""

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

    # 선택한 revision이 여전히 일치할 때만 진행·오류·요약을 반영하고 불일치는 None으로 알린다.
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

    # 최신 생성 순으로 페이지를 구성하며 저장된 요약 JSON을 객체로 풀어 반환한다.
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
