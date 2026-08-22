"""The only repository allowed to open the AI_CCTV SQLite database."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

from ai_cctv_core.time import format_utc, parse_utc, utc_now

from .database import Database


def _now() -> str:
    return format_utc(utc_now())


def _as_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else dict(row)


def _user(row: sqlite3.Row | None) -> dict[str, Any] | None:
    result = _as_dict(row)
    if result is not None:
        result["is_active"] = bool(result["is_active"])
    return result


def _camera(row: sqlite3.Row | None) -> dict[str, Any] | None:
    result = _as_dict(row)
    if result is not None:
        result["enabled"] = bool(result["enabled"])
    return result


def _event(row: sqlite3.Row | None) -> dict[str, Any] | None:
    result = _as_dict(row)
    if result is not None:
        result["metadata"] = json.loads(result.pop("metadata_json"))
    return result


class DataRepository:
    def __init__(self, database: Database) -> None:
        self.database = database

    def initialize(self) -> None:
        self.database.initialize()

    # Users and camera permissions
    def user_count(self) -> int:
        with self.database.connection() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM users").fetchone()[0])

    def create_user(self, values: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO users(
                    username, password_hash, role, is_active, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    values["username"],
                    values["password_hash"],
                    values["role"],
                    int(values.get("is_active", True)),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM users WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return _user(row) or {}

    def get_user(self, user_id: int) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            return _user(
                connection.execute(
                    "SELECT * FROM users WHERE id = ?", (user_id,)
                ).fetchone()
            )

    def get_user_by_username(self, username: str) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            return _user(
                connection.execute(
                    "SELECT * FROM users WHERE username = ?", (username,)
                ).fetchone()
            )

    def list_users(self, limit: int, offset: int) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM users ORDER BY id LIMIT ? OFFSET ?", (limit, offset)
            ).fetchall()
        return [_user(row) or {} for row in rows]

    def update_user(
        self, user_id: int, values: dict[str, Any]
    ) -> dict[str, Any] | None:
        allowed = {"username", "password_hash", "role", "is_active"}
        changes = {key: value for key, value in values.items() if key in allowed}
        if "is_active" in changes:
            changes["is_active"] = int(changes["is_active"])
        if not changes:
            return self.get_user(user_id)
        changes["updated_at"] = _now()
        assignments = ", ".join(f"{column} = ?" for column in changes)
        with self.database.transaction() as connection:
            cursor = connection.execute(
                f"UPDATE users SET {assignments} WHERE id = ?",
                (*changes.values(), user_id),
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return _user(row)

    def delete_user(self, user_id: int) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute("DELETE FROM users WHERE id = ?", (user_id,))
            return cursor.rowcount > 0

    def grant_camera(self, user_id: int, camera_id: str) -> dict[str, Any] | None:
        now = _now()
        with self.database.transaction() as connection:
            camera = connection.execute(
                "SELECT id FROM cameras WHERE camera_id = ?", (camera_id,)
            ).fetchone()
            if camera is None:
                return None
            connection.execute(
                """
                INSERT INTO user_camera_permissions(user_id, camera_id, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id, camera_id) DO NOTHING
                """,
                (user_id, camera["id"], now),
            )
        return {"user_id": user_id, "camera_id": camera_id, "created_at": now}

    def revoke_camera(self, user_id: int, camera_id: str) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                DELETE FROM user_camera_permissions
                WHERE user_id = ?
                  AND camera_id = (SELECT id FROM cameras WHERE camera_id = ?)
                """,
                (user_id, camera_id),
            )
            return cursor.rowcount > 0

    def list_user_cameras(self, user_id: int) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT c.* FROM cameras c
                JOIN user_camera_permissions p ON p.camera_id = c.id
                WHERE p.user_id = ?
                ORDER BY c.camera_id
                """,
                (user_id,),
            ).fetchall()
        return [_camera(row) or {} for row in rows]

    # Cameras
    def camera_count(self) -> int:
        with self.database.connection() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM cameras").fetchone()[0])

    def create_camera(self, values: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        with self.database.transaction() as connection:
            cursor = connection.execute(
                """
                INSERT INTO cameras(
                    camera_id, name, stream_path, edge_device_id, source_url,
                    enabled, status, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["camera_id"],
                    values["name"],
                    values["stream_path"],
                    values.get("edge_device_id"),
                    values.get("source_url"),
                    int(values.get("enabled", True)),
                    values.get("status", "offline"),
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM cameras WHERE id = ?", (cursor.lastrowid,)
            ).fetchone()
        return _camera(row) or {}

    def get_camera(self, camera_id: str) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            return _camera(
                connection.execute(
                    "SELECT * FROM cameras WHERE camera_id = ?", (camera_id,)
                ).fetchone()
            )

    def list_cameras(
        self,
        limit: int,
        offset: int,
        *,
        enabled_only: bool = False,
        user_id: int | None = None,
    ) -> list[dict[str, Any]]:
        where: list[str] = []
        parameters: list[Any] = []
        join = ""
        with self.database.connection() as connection:
            if user_id is not None:
                user = connection.execute(
                    "SELECT role FROM users WHERE id = ?", (user_id,)
                ).fetchone()
                if user is None:
                    return []
                if user["role"] != "admin":
                    join = "JOIN user_camera_permissions p ON p.camera_id = c.id"
                    where.append("p.user_id = ?")
                    parameters.append(user_id)
            if enabled_only:
                where.append("c.enabled = 1")
            clause = " WHERE " + " AND ".join(where) if where else ""
            rows = connection.execute(
                f"SELECT c.* FROM cameras c {join}{clause} "
                "ORDER BY c.camera_id LIMIT ? OFFSET ?",
                (*parameters, limit, offset),
            ).fetchall()
        return [_camera(row) or {} for row in rows]

    def update_camera(
        self, camera_id: str, values: dict[str, Any]
    ) -> dict[str, Any] | None:
        allowed = {
            "name",
            "stream_path",
            "edge_device_id",
            "source_url",
            "enabled",
            "status",
        }
        changes = {key: value for key, value in values.items() if key in allowed}
        if "enabled" in changes:
            changes["enabled"] = int(changes["enabled"])
        if not changes:
            return self.get_camera(camera_id)
        changes["updated_at"] = _now()
        assignments = ", ".join(f"{column} = ?" for column in changes)
        with self.database.transaction() as connection:
            cursor = connection.execute(
                f"UPDATE cameras SET {assignments} WHERE camera_id = ?",
                (*changes.values(), camera_id),
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                "SELECT * FROM cameras WHERE camera_id = ?", (camera_id,)
            ).fetchone()
        return _camera(row)

    def delete_camera(self, camera_id: str) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM cameras WHERE camera_id = ?", (camera_id,)
            )
            return cursor.rowcount > 0

    def put_camera_publish_credential(
        self, camera_id: str, username: str, password_hash: str
    ) -> dict[str, Any] | None:
        if self.get_camera(camera_id) is None:
            return None
        now = _now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO camera_publish_credentials(
                    camera_id, username, password_hash, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(camera_id) DO UPDATE SET
                    username = excluded.username,
                    password_hash = excluded.password_hash,
                    updated_at = excluded.updated_at
                """,
                (camera_id, username, password_hash, now, now),
            )
            row = connection.execute(
                "SELECT * FROM camera_publish_credentials WHERE camera_id = ?",
                (camera_id,),
            ).fetchone()
        return _as_dict(row)

    def get_camera_publish_credential(self, camera_id: str) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            return _as_dict(
                connection.execute(
                    "SELECT * FROM camera_publish_credentials WHERE camera_id = ?",
                    (camera_id,),
                ).fetchone()
            )

    # Recording segments
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

    # Events
    def create_event(self, values: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        explicit_ids = {int(value) for value in values.get("recording_segment_ids", [])}
        if values.get("recording_segment_id") is not None:
            explicit_ids.add(int(values["recording_segment_id"]))
        with self.database.transaction() as connection:
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
            primary_id = values.get("recording_segment_id")
            if primary_id is None and segment_ids:
                primary_id = min(segment_ids)
            cursor = connection.execute(
                """
                INSERT INTO events(
                    camera_id, event_type, occurred_at, person_id, track_id,
                    confidence, recording_segment_id, snapshot_path,
                    metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["camera_id"],
                    values["event_type"],
                    values["occurred_at"],
                    values.get("person_id"),
                    values.get("track_id"),
                    values.get("confidence"),
                    primary_id,
                    values.get("snapshot_path"),
                    json.dumps(values.get("metadata", {}), ensure_ascii=False),
                    now,
                ),
            )
            event_id = int(cursor.lastrowid)
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

    # Refresh and revoked token state
    def issue_refresh_token(self, values: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        with self.database.transaction() as connection:
            rotated_from = values.get("rotated_from_jti")
            if rotated_from:
                previous = connection.execute(
                    "SELECT * FROM refresh_tokens WHERE jti = ?", (rotated_from,)
                ).fetchone()
                if previous is None:
                    raise LookupError("refresh token to rotate was not found")
                if previous["revoked_at"] is not None or previous["expires_at"] <= now:
                    raise PermissionError("refresh token is revoked or expired")
                if int(previous["user_id"]) != int(values["user_id"]):
                    raise PermissionError("refresh token owner does not match")
            connection.execute(
                """
                INSERT INTO refresh_tokens(
                    user_id, jti, token_hash, family_id, expires_at,
                    rotated_from_jti, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    values["user_id"],
                    values["jti"],
                    values["token_hash"],
                    values.get("family_id"),
                    values["expires_at"],
                    rotated_from,
                    now,
                ),
            )
            if rotated_from:
                connection.execute(
                    """
                    UPDATE refresh_tokens
                    SET revoked_at = ?, replaced_by_jti = ?
                    WHERE jti = ?
                    """,
                    (now, values["jti"], rotated_from),
                )
            row = connection.execute(
                "SELECT * FROM refresh_tokens WHERE jti = ?", (values["jti"],)
            ).fetchone()
        return dict(row)

    def get_refresh_token(self, jti: str) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            return _as_dict(
                connection.execute(
                    "SELECT * FROM refresh_tokens WHERE jti = ?", (jti,)
                ).fetchone()
            )

    def delete_refresh_token(self, jti: str) -> bool:
        with self.database.transaction() as connection:
            cursor = connection.execute(
                "DELETE FROM refresh_tokens WHERE jti = ?", (jti,)
            )
            return cursor.rowcount > 0

    def put_revoked_token(self, jti: str, values: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO revoked_tokens(jti, user_id, expires_at, revoked_at, reason)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(jti) DO UPDATE SET
                    user_id = excluded.user_id,
                    expires_at = excluded.expires_at,
                    reason = excluded.reason
                """,
                (
                    jti,
                    values.get("user_id"),
                    values["expires_at"],
                    now,
                    values.get("reason"),
                ),
            )
            row = connection.execute(
                "SELECT * FROM revoked_tokens WHERE jti = ?", (jti,)
            ).fetchone()
        return dict(row)

    def get_revoked_token(self, jti: str) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            return _as_dict(
                connection.execute(
                    "SELECT * FROM revoked_tokens WHERE jti = ?", (jti,)
                ).fetchone()
            )
