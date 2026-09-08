# 이벤트와 관련 녹화, 알림·객체 작업을 함께 저장하여 일부만 기록되는 상황을 막는다.
"""Events persistence and SQL operations."""

from __future__ import annotations

import json
from typing import Any

from ..connection import Database
from .base import _event, _now


class EventsRepositoryMixin:
    database: Database

    def create_event(self, values: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        explicit_ids = {int(value) for value in values.get("recording_segment_ids", [])}
        if values.get("recording_segment_id") is not None:
            explicit_ids.add(int(values["recording_segment_id"]))
        with self.database.transaction() as connection:
            edge_event_id = values.get("edge_event_id")
            if edge_event_id is not None:
                existing = connection.execute(
                    """
                    SELECT id FROM events
                    WHERE camera_id = ? AND edge_event_id = ?
                    """,
                    (values["camera_id"], edge_event_id),
                ).fetchone()
                if existing is not None:
                    existing_id = int(existing["id"])
                    # 같은 Edge 이벤트를 다시 받으면 기존 결과를 반환하여 후속 작업 중복을 막는다.
                    return self.get_event(existing_id) or {}
            automatic = connection.execute(
                """
                SELECT id FROM recording_segments
                WHERE camera_id = ?
                  AND start_time < ?
                  AND end_time > ?
                  AND status = 'ready'
                ORDER BY start_time, id
                """,
                (
                    values["camera_id"],
                    values.get("link_end_at", values["occurred_at"]),
                    values.get("link_start_at", values["occurred_at"]),
                ),
            ).fetchall()
            segment_ids = explicit_ids | {int(row["id"]) for row in automatic}
            if segment_ids:
                placeholders = ",".join("?" for _ in segment_ids)
                matching = connection.execute(
                    f"SELECT id FROM recording_segments WHERE camera_id = ? "
                    f"AND id IN ({placeholders})",
                    (values["camera_id"], *sorted(segment_ids)),
                ).fetchall()
                matching_ids = {int(row["id"]) for row in matching}
                if matching_ids != segment_ids:
                    raise ValueError(
                        "recording segments must exist and belong to the event camera"
                    )
            session = values.get("metadata", {}).get("tracking_session_id")
            if session and values.get("person_id"):
                identity = connection.execute(
                    "SELECT global_person_id FROM person_identity_links WHERE camera_id=? AND tracking_session_id=? AND person_id=?",
                    (values["camera_id"], session, values["person_id"]),
                ).fetchone()
                if identity:
                    values["global_person_id"] = identity["global_person_id"]
            primary_id = values.get("recording_segment_id")
            if primary_id is None and segment_ids:
                primary_id = min(segment_ids)
            cursor = connection.execute(
                """
                INSERT INTO events(
                    camera_id, event_type, occurred_at, person_id, global_person_id,
                    confidence, recording_segment_id, snapshot_path,
                    metadata_json, edge_event_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["camera_id"],
                    values["event_type"],
                    values["occurred_at"],
                    values.get("person_id"),
                    values.get("global_person_id"),
                    values.get("confidence"),
                    primary_id,
                    values.get("snapshot_path"),
                    json.dumps(values.get("metadata", {}), ensure_ascii=False),
                    edge_event_id,
                    now,
                ),
            )
            event_id = int(cursor.lastrowid)
            # 이벤트와 발송·분석 예약을 같은 트랜잭션에 넣는다.
            # 따라서 이벤트만 저장되고 후속 작업이 사라지는 중간 상태가 남지 않는다.
            self._enqueue_push(connection, event_id)
            self._enqueue_object_jobs(connection, event_id, values)
            connection.executemany(
                """
                INSERT INTO event_recording_segments(
                    event_id, recording_segment_id, created_at
                ) VALUES (?, ?, ?)
                """,
                [(event_id, segment_id, now) for segment_id in sorted(segment_ids)],
            )
        result = self.get_event(event_id)
        if result is None:
            raise RuntimeError("event disappeared after creation")
        return result

    def get_event(self, event_id: int) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM events WHERE id = ?", (event_id,)
            ).fetchone()
            result = _event(row)
            if result is None:
                return None
            links = connection.execute(
                """
                SELECT recording_segment_id FROM event_recording_segments
                WHERE event_id = ? ORDER BY recording_segment_id
                """,
                (event_id,),
            ).fetchall()
        result["recording_segment_ids"] = [
            int(link["recording_segment_id"]) for link in links
        ]
        return result

    def search_events(
        self,
        *,
        camera_id: str | None,
        event_type: str | None,
        start_time: str | None,
        end_time: str | None,
        limit: int,
        offset: int,
    ) -> list[dict[str, Any]]:
        conditions: list[str] = []
        parameters: list[Any] = []
        if camera_id is not None:
            conditions.append("camera_id = ?")
            parameters.append(camera_id)
        if event_type is not None:
            conditions.append("event_type = ?")
            parameters.append(event_type)
        if start_time is not None:
            conditions.append("occurred_at >= ?")
            parameters.append(start_time)
        if end_time is not None:
            conditions.append("occurred_at < ?")
            parameters.append(end_time)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self.database.connection() as connection:
            rows = connection.execute(
                f"SELECT id FROM events{where} "
                "ORDER BY occurred_at, id LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            ).fetchall()
        return [self.get_event(int(row["id"])) or {} for row in rows]
