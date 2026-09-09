# 녹화 파일의 경로·시간·상태를 저장하여 영상 검색과 이벤트 연결을 지원한다.

from __future__ import annotations

import sqlite3
from datetime import timedelta
from typing import Any

from ai_cctv_core.time import format_utc, parse_utc

from ..connection import Database
from .base import _as_dict, _now


def _link_segment_to_events(
    connection: sqlite3.Connection, segment: dict[str, Any],
    pre_roll_seconds: int, post_roll_seconds: int,
) -> None:
    if segment["status"] != "ready":
        return
    lower = format_utc(parse_utc(segment["start_time"]) - timedelta(seconds=post_roll_seconds))
    upper = format_utc(parse_utc(segment["end_time"]) + timedelta(seconds=pre_roll_seconds))
    connection.execute(
        "INSERT OR IGNORE INTO event_recording_segments(event_id,recording_segment_id,created_at) "
        "SELECT id,?,? FROM events WHERE camera_id=? AND occurred_at>? AND occurred_at<?",
        (segment["id"], _now(), segment["camera_id"], lower, upper),
    )
    connection.execute(
        "UPDATE events SET recording_segment_id=? WHERE recording_segment_id IS NULL "
        "AND camera_id=? AND occurred_at>? AND occurred_at<?",
        (segment["id"], segment["camera_id"], lower, upper),
    )


class RecordingsRepositoryMixin:
    database: Database

    # 멱등 키와 파일 경로로 중복을 확인하고 반환 불리언으로 실제 신규 생성 여부를 알린다.
    def create_segment(
        self, values: dict[str, Any], *,
        pre_roll_seconds: int | None = None,
        post_roll_seconds: int | None = None,
    ) -> tuple[dict[str, Any], bool]:
        if (pre_roll_seconds is None) != (post_roll_seconds is None):
            raise ValueError("both event roll settings are required")
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
                segment = dict(existing)
                if pre_roll_seconds is not None:
                    _link_segment_to_events(connection, segment, pre_roll_seconds, post_roll_seconds)
                return segment, False
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
            if pre_roll_seconds is not None:
                _link_segment_to_events(connection, dict(row), pre_roll_seconds, post_roll_seconds)
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

    # 요청 구간과 양의 길이로 겹치는 녹화를 찾으며 삭제 확정 상태만 제외한다.
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

    # 삭제 중·누락·손상 상태도 포함하여 파일 대조 작업에서 회복 여부를 판단하게 한다.
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

    # 녹화 구간을 기준으로 post-roll은 왼쪽, pre-roll은 오른쪽으로 이벤트 조회 범위를 확장한다.
    def link_segment_to_events(
        self,
        segment: dict[str, Any],
        pre_roll_seconds: int,
        post_roll_seconds: int,
    ) -> None:
        """새 녹화 조각을 시간대가 겹치는 기존 이벤트에 연결한다."""

        with self.database.transaction() as connection:
            _link_segment_to_events(connection, segment, pre_roll_seconds, post_roll_seconds)

    def repair_event_recording_links(self, pre_roll_seconds: int, post_roll_seconds: int) -> int:
        """구형 버전에서 남은 연결 누락도 실행당 최대 1,000개씩 회복한다."""
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT e.id AS event_id,s.id AS segment_id FROM recording_segments s "
                "JOIN events e ON e.camera_id=s.camera_id "
                "AND e.occurred_at>strftime('%Y-%m-%dT%H:%M:%fZ',s.start_time,?) "
                "AND e.occurred_at<strftime('%Y-%m-%dT%H:%M:%fZ',s.end_time,?) "
                "WHERE s.status='ready' AND NOT EXISTS(SELECT 1 FROM event_recording_segments l "
                "WHERE l.event_id=e.id AND l.recording_segment_id=s.id) ORDER BY s.id,e.id LIMIT 1000",
                (f"-{post_roll_seconds} seconds", f"+{pre_roll_seconds} seconds"),
            ).fetchall()
            connection.executemany(
                "INSERT INTO event_recording_segments(event_id,recording_segment_id,created_at) VALUES (?,?,?)",
                [(row["event_id"], row["segment_id"], _now()) for row in rows],
            )
            connection.executemany(
                "UPDATE events SET recording_segment_id=? WHERE id=? AND recording_segment_id IS NULL",
                [(row["segment_id"], row["event_id"]) for row in rows],
            )
            return len(rows)

    # 기준 시각보다 먼저 끝난 녹화만 선택하고 이미 삭제 중인 항목은 중복 처리하지 않는다.
    def retention_candidates(self, cutoff: str) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT * FROM recording_segments
                WHERE end_time < ? AND status NOT IN ('writing', 'deleting', 'deleted')
                ORDER BY end_time, id
                LIMIT 1000
                """,
                (cutoff,),
            ).fetchall()
        return [dict(row) for row in rows]
