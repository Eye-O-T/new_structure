# 이벤트와 관련 녹화, 알림·객체 작업을 함께 저장하여 일부만 기록되는 상황을 막는다.

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ..connection import Database
from .base import _event, _now
from .identity import note_track_observation


def _event_details(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
    result = _event(row)
    assert result is not None
    event_id = int(row["id"])
    links = connection.execute(
        """
        SELECT recording_segment_id FROM event_recording_segments
        WHERE event_id = ? ORDER BY recording_segment_id
        """,
        (event_id,),
    ).fetchall()
    # 이전 버전에서 남은 metadata 불일치도 현재 작업 상태로 일관되게 읽는다.
    jobs = connection.execute(
        "SELECT stage,state,updated_at FROM object_jobs WHERE event_id=?",
        (event_id,),
    ).fetchall()
    for job in jobs:
        stage_metadata = dict(result["metadata"].get(job["stage"], {}))
        stage_metadata.update(status=job["state"], updated_at=job["updated_at"])
        result["metadata"][job["stage"]] = stage_metadata
    result["recording_segment_ids"] = [
        int(link["recording_segment_id"]) for link in links
    ]
    return result


class EventsRepositoryMixin:
    database: Database

    # Edge 이벤트 재전송을 식별하고 녹화 연결·푸시·객체 작업을 한 번에 저장한다.
    def create_event(self, values: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        explicit_ids = {int(value) for value in values.get("recording_segment_ids", [])}
        if values.get("recording_segment_id") is not None:
            explicit_ids.add(int(values["recording_segment_id"]))
        with self.database.transaction() as connection:
            edge_event_id = values.get("edge_event_id")
            source_event_id = values.get("source_event_id")
            if source_event_id is not None:
                existing = connection.execute(
                    "SELECT id FROM events WHERE camera_id=? AND source_event_id=?",
                    (values["camera_id"], source_event_id),
                ).fetchone()
                if existing is not None:
                    # 응답이 유실된 전처리 이벤트 재전송은 푸시와 객체 작업도 재생성하지 않는다.
                    return self.get_event(int(existing["id"])) or {}
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
            if values["event_type"] in {
                "central_connection_lost", "central_connection_restored"
            }:
                origin = connection.execute(
                    "SELECT e.edge_device_id FROM cameras c "
                    "JOIN edge_devices e ON e.edge_device_id=c.edge_device_id "
                    "WHERE c.camera_id=?", (values["camera_id"],)
                ).fetchone()
                values["metadata"] = dict(values.get("metadata", {}))
                values["metadata"]["recovery_edge_device_id"] = (
                    origin["edge_device_id"] if origin is not None else None
                )
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
                    metadata_json, edge_event_id, source_event_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    source_event_id,
                    now,
                ),
            )
            event_id = int(cursor.lastrowid)
            if session and values.get("person_id") and values["event_type"] in {
                "person_appeared", "person_disappeared"
            }:
                note_track_observation(
                    connection, values["camera_id"], session, values["person_id"],
                    values["occurred_at"], ended=values["event_type"] == "person_disappeared",
                )
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

    # 단일 대표 녹화와 함께 다대다 연결의 전체 녹화 ID를 돌려준다.
    def get_event(self, event_id: int) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT * FROM events WHERE id = ?", (event_id,)
            ).fetchone()
            if row is None:
                return None
            return _event_details(connection, row)

    # 시작은 포함하고 종료는 제외하는 시간 조건으로 검색하여 시간·ID 순 페이지를 만든다.
    def event_snapshot_max_id(self) -> int:
        with self.database.connection() as connection:
            return int(connection.execute("SELECT coalesce(max(id), 0) FROM events").fetchone()[0])

    def search_events(
        self,
        *,
        camera_id: str | None,
        event_type: str | None,
        start_time: str | None,
        end_time: str | None,
        limit: int,
        offset: int,
        snapshot_max_id: int | None = None,
        after_time: str | None = None,
        after_id: int | None = None,
        descending: bool = False,
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
        if snapshot_max_id is not None:
            conditions.append("id <= ?")
            parameters.append(snapshot_max_id)
        if after_time is not None and after_id is not None:
            operator = "<" if descending else ">"
            conditions.append(f"(occurred_at {operator} ? OR (occurred_at = ? AND id {operator} ?))")
            parameters.extend((after_time, after_time, after_id))
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        order = "DESC" if descending else "ASC"
        with self.database.connection() as connection:
            # 보존 작업이 동시에 삭제해도 선택한 행과 연결 정보는 한 스냅샷에서 읽는다.
            connection.execute("BEGIN")
            rows = connection.execute(
                f"SELECT * FROM events{where} "
                f"ORDER BY occurred_at {order}, id {order} LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            ).fetchall()
            return [_event_details(connection, row) for row in rows]
