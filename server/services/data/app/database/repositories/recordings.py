"""Recordings persistence and SQL operations."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from ai_cctv_core.time import format_utc, parse_utc

from ..connection import Database
from .base import _as_dict, _now


class RecordingsRepositoryMixin:
    database: Database

    def create_segment(self, values: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        now = _now()
        with self.database.transaction() as connection:
            existing = None
            if values.get("idempotency_key"):
                existing = connection.execute(
                    "SELECT * FROM recording_segments WHERE idempotency_key = ?",
                    (values["idempotency_key"],),
                ).fetchone()
            if existing is None:
                existing = connection.execute(
                    "SELECT * FROM recording_segments WHERE relative_path = ?",
                    (values["relative_path"],),
                ).fetchone()
            if existing is not None:
                return dict(existing), False
            cursor = connection.execute(
                """
                INSERT INTO recording_segments(
                    camera_id, start_time, end_time, relative_path, format, codec,
                    duration_ms, file_size, source, status, checksum,
                    idempotency_key, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["camera_id"],
                    values["start_time"],
                    values["end_time"],
                    values["relative_path"],
                    values["format"],
                    values.get("codec", "h264"),
                    values["duration_ms"],
                    values["file_size"],
                    values["source"],
                    values["status"],
                    values.get("checksum"),
                    values.get("idempotency_key"),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM recording_segments WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return dict(row), True

    def get_segment(self, segment_id: int) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            return _as_dict(
                connection.execute(
                    "SELECT * FROM recording_segments WHERE id = ?", (segment_id,)
                ).fetchone()
            )

    def get_segment_by_path(self, relative_path: str) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            return _as_dict(
                connection.execute(
                    "SELECT * FROM recording_segments WHERE relative_path = ?",
                    (relative_path,),
                ).fetchone()
            )

    def search_segments(
        self,
        camera_id: str,
        start_time: str,
        end_time: str,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM recording_segments
                WHERE camera_id = ?
                  AND start_time < ?
                  AND end_time > ?
                  AND status != 'deleted'
                ORDER BY start_time, id
                LIMIT ? OFFSET ?
                """,
                (camera_id, end_time, start_time, limit, offset),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_segments_for_reconcile(self) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM recording_segments WHERE status != 'deleted' ORDER BY id"
            ).fetchall()
        return [dict(row) for row in rows]

    def set_segment_status(self, segment_id: int, status: str) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "UPDATE recording_segments SET status = ?, updated_at = ? WHERE id = ?",
                (status, _now(), segment_id),
            )

    def link_segment_to_events(
        self,
        segment: dict[str, Any],
        pre_roll_seconds: int,
        post_roll_seconds: int,
    ) -> None:
        """Attach a newly indexed segment to existing event playback windows."""

        segment_start = parse_utc(segment["start_time"])
        segment_end = parse_utc(segment["end_time"])
        event_lower = format_utc(segment_start - timedelta(seconds=post_roll_seconds))
        event_upper = format_utc(segment_end + timedelta(seconds=pre_roll_seconds))
        now = _now()
        with self.database.transaction() as connection:
            events = connection.execute(
                """
                SELECT id FROM events
                WHERE camera_id = ?
                  AND occurred_at >= ?
                  AND occurred_at < ?
                """,
                (segment["camera_id"], event_lower, event_upper),
            ).fetchall()
            connection.executemany(
                """
                INSERT OR IGNORE INTO event_recording_segments(
                    event_id, recording_segment_id, created_at
                ) VALUES (?, ?, ?)
                """,
                [(int(event["id"]), int(segment["id"]), now) for event in events],
            )
            if events:
                placeholders = ",".join("?" for _ in events)
                connection.execute(
                    f"""
                    UPDATE events SET recording_segment_id = ?
                    WHERE recording_segment_id IS NULL
                      AND id IN ({placeholders})
                    """,
                    (int(segment["id"]), *(int(event["id"]) for event in events)),
                )

    def retention_candidates(self, cutoff: str) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM recording_segments
                WHERE end_time < ? AND status NOT IN ('deleting', 'deleted')
                ORDER BY end_time, id
                """,
                (cutoff,),
            ).fetchall()
        return [dict(row) for row in rows]
