"""The only repository allowed to open the AI_CCTV SQLite database."""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from typing import Any

from ai_cctv_core.time import format_utc, parse_utc, utc_now

from .database import Database


RECOVERY_EVENT_CORRELATION_SECONDS = 60


class CameraLimitReached(Exception):
    pass


class CameraHasHistory(Exception):
    pass


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


def _video_profile(row: sqlite3.Row | None) -> dict[str, Any] | None:
    result = _as_dict(row)
    if result is not None:
        result["supported_profiles"] = json.loads(result.pop("supported_profiles_json"))
        if "edge_online" in result:
            result["edge_online"] = bool(result["edge_online"])
    return result


def _runtime_status(row: sqlite3.Row | None) -> dict[str, Any] | None:
    result = _as_dict(row)
    if result is not None and "online" in result:
        result["online"] = bool(result["online"])
        if result.get("online_observed") is not None:
            result["online_observed"] = bool(result["online_observed"])
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

    def replace_camera_permissions(
        self, user_id: int, camera_ids: list[str]
    ) -> list[dict[str, Any]]:
        """Validate the complete target set, then replace it atomically."""

        unique_ids = list(dict.fromkeys(camera_ids))
        now = _now()
        with self.database.transaction() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM users WHERE id = ?", (user_id,)
                ).fetchone()
                is None
            ):
                raise LookupError("user not found")
            cameras: dict[str, int] = {}
            if unique_ids:
                placeholders = ",".join("?" for _ in unique_ids)
                rows = connection.execute(
                    f"SELECT id, camera_id FROM cameras "
                    f"WHERE camera_id IN ({placeholders})",
                    tuple(unique_ids),
                ).fetchall()
                cameras = {str(row["camera_id"]): int(row["id"]) for row in rows}
                if set(cameras) != set(unique_ids):
                    raise ValueError("one or more cameras were not found")
            connection.execute(
                "DELETE FROM user_camera_permissions WHERE user_id = ?", (user_id,)
            )
            connection.executemany(
                """
                INSERT INTO user_camera_permissions(user_id, camera_id, created_at)
                VALUES (?, ?, ?)
                """,
                [(user_id, cameras[camera_id], now) for camera_id in unique_ids],
            )
        return self.list_user_cameras(user_id)

    # Cameras
    def camera_count(self) -> int:
        with self.database.connection() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM cameras").fetchone()[0])

    def create_camera(self, values: dict[str, Any]) -> dict[str, Any]:
        now = _now()
        with self.database.transaction() as connection:
            enabled_camera_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM cameras WHERE enabled = 1"
                ).fetchone()[0]
            )
            if values.get("enabled", True) and enabled_camera_count >= 4:
                raise CameraLimitReached
            edge_device_id = values.get("edge_device_id")
            management_url = values.get("edge_management_url")
            recovery_url = values.get("edge_recovery_url")
            auth_token = values.get("edge_auth_token")
            if edge_device_id and management_url and recovery_url and auth_token:
                connection.execute(
                    """
                    INSERT INTO edge_devices(
                        edge_device_id, management_url, recovery_url, auth_token,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(edge_device_id) DO UPDATE SET
                        management_url = excluded.management_url,
                        recovery_url = excluded.recovery_url,
                        auth_token = excluded.auth_token,
                        updated_at = excluded.updated_at
                    """,
                    (
                        edge_device_id,
                        management_url,
                        recovery_url,
                        auth_token,
                        now,
                        now,
                    ),
                )
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
                    edge_device_id,
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
            connection.execute(
                """
                INSERT INTO camera_runtime_status(camera_id, updated_at)
                VALUES (?, ?)
                """,
                (values["camera_id"], now),
            )
            connection.execute(
                """
                INSERT INTO camera_video_profiles(camera_id, created_at, updated_at)
                VALUES (?, ?, ?)
                """,
                (values["camera_id"], now, now),
            )
        return _camera(row) or {}

    def get_camera(self, camera_id: str) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            return _camera(
                connection.execute(
                    "SELECT * FROM cameras WHERE camera_id = ?", (camera_id,)
                ).fetchone()
            )

    def get_camera_deletion_status(self, camera_id: str) -> dict[str, Any] | None:
        """Report whether a camera can be deleted without losing history."""

        with self.database.connection() as connection:
            camera = connection.execute(
                "SELECT 1 FROM cameras WHERE camera_id = ?", (camera_id,)
            ).fetchone()
            if camera is None:
                return None
            has_history = bool(
                connection.execute(
                    """
                    SELECT EXISTS(
                        SELECT 1 FROM recording_segments WHERE camera_id = ?
                        UNION ALL
                        SELECT 1 FROM events WHERE camera_id = ?
                        UNION ALL
                        SELECT 1 FROM recovery_jobs WHERE camera_id = ?
                    )
                    """,
                    (camera_id, camera_id, camera_id),
                ).fetchone()[0]
            )
        return {
            "camera_id": camera_id,
            "deletable": not has_history,
            "reason_code": "CAMERA_HAS_HISTORY" if has_history else None,
        }

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
        now = _now()
        changes["updated_at"] = now
        assignments = ", ".join(f"{column} = ?" for column in changes)
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM cameras WHERE camera_id = ?", (camera_id,)
            ).fetchone()
            if existing is None:
                return None
            if (
                values.get("enabled") is True
                and not bool(existing["enabled"])
                and int(
                    connection.execute(
                        "SELECT COUNT(*) FROM cameras WHERE enabled = 1"
                    ).fetchone()[0]
                )
                >= 4
            ):
                raise CameraLimitReached
            edge_device_id = values.get("edge_device_id", existing["edge_device_id"])
            edge_fields_supplied = any(
                key in values
                for key in (
                    "edge_device_id",
                    "edge_management_url",
                    "edge_recovery_url",
                    "edge_auth_token",
                )
            )
            if edge_fields_supplied:
                if not edge_device_id:
                    raise ValueError("edge_device_id is required for Edge metadata")
                registered = connection.execute(
                    "SELECT * FROM edge_devices WHERE edge_device_id = ?",
                    (edge_device_id,),
                ).fetchone()
                management_url = values.get(
                    "edge_management_url",
                    registered["management_url"] if registered is not None else None,
                )
                recovery_url = values.get(
                    "edge_recovery_url",
                    registered["recovery_url"] if registered is not None else None,
                )
                auth_token = values.get(
                    "edge_auth_token",
                    registered["auth_token"] if registered is not None else None,
                )
                if management_url is None or recovery_url is None or auth_token is None:
                    raise ValueError(
                        "complete Edge device metadata is required for a new device"
                    )
                connection.execute(
                    """
                    INSERT INTO edge_devices(
                        edge_device_id, management_url, recovery_url, auth_token,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(edge_device_id) DO UPDATE SET
                        management_url = excluded.management_url,
                        recovery_url = excluded.recovery_url,
                        auth_token = excluded.auth_token,
                        updated_at = excluded.updated_at
                    """,
                    (
                        edge_device_id,
                        management_url,
                        recovery_url,
                        auth_token,
                        now,
                        now,
                    ),
                )
            cursor = connection.execute(
                f"UPDATE cameras SET {assignments} WHERE camera_id = ?",
                (*changes.values(), camera_id),
            )
            if cursor.rowcount == 0:
                return None
            row = connection.execute(
                "SELECT * FROM cameras WHERE camera_id = ?", (camera_id,)
            ).fetchone()
            old_edge_device_id = existing["edge_device_id"]
            if old_edge_device_id is not None and old_edge_device_id != edge_device_id:
                connection.execute(
                    """
                    DELETE FROM edge_devices
                    WHERE edge_device_id = ?
                      AND NOT EXISTS(
                          SELECT 1 FROM cameras WHERE edge_device_id = ?
                      )
                    """,
                    (old_edge_device_id, old_edge_device_id),
                )
        return _camera(row)

    def get_edge_device(self, edge_device_id: str) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            return _as_dict(
                connection.execute(
                    "SELECT * FROM edge_devices WHERE edge_device_id = ?",
                    (edge_device_id,),
                ).fetchone()
            )

    def put_edge_device(
        self,
        edge_device_id: str,
        management_url: str,
        recovery_url: str,
        auth_token: str,
    ) -> dict[str, Any]:
        now = _now()
        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO edge_devices(
                    edge_device_id, management_url, recovery_url, auth_token,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(edge_device_id) DO UPDATE SET
                    management_url = excluded.management_url,
                    recovery_url = excluded.recovery_url,
                    auth_token = excluded.auth_token,
                    updated_at = excluded.updated_at
                """,
                (edge_device_id, management_url, recovery_url, auth_token, now, now),
            )
            row = connection.execute(
                "SELECT * FROM edge_devices WHERE edge_device_id = ?",
                (edge_device_id,),
            ).fetchone()
        return _as_dict(row) or {}

    def get_camera_control_target(self, camera_id: str) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            return _as_dict(
                connection.execute(
                    """
                    SELECT c.camera_id, c.edge_device_id, e.management_url,
                           e.auth_token
                    FROM cameras c
                    JOIN edge_devices e ON e.edge_device_id = c.edge_device_id
                    WHERE c.camera_id = ?
                    """,
                    (camera_id,),
                ).fetchone()
            )

    def list_camera_control_targets(self) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT c.camera_id, c.edge_device_id, e.management_url,
                       e.auth_token, r.event_cursor
                FROM cameras c
                JOIN edge_devices e ON e.edge_device_id = c.edge_device_id
                LEFT JOIN camera_runtime_status r ON r.camera_id = c.camera_id
                WHERE c.enabled = 1
                ORDER BY c.camera_id
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def get_camera_video_profile(self, camera_id: str) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT p.camera_id, p.current_profile, p.desired_profile,
                       p.supported_profiles_json, p.encoder, p.last_error_code,
                       COALESCE(e.online, 0) AS edge_online, p.updated_at
                FROM camera_video_profiles p
                LEFT JOIN cameras c ON c.camera_id = p.camera_id
                LEFT JOIN edge_runtime_status e
                       ON e.edge_device_id = c.edge_device_id
                WHERE p.camera_id = ?
                """,
                (camera_id,),
            ).fetchone()
        return _video_profile(row)

    def update_camera_video_profile(
        self, camera_id: str, values: dict[str, Any]
    ) -> dict[str, Any] | None:
        if self.get_camera(camera_id) is None:
            return None
        allowed = {
            "desired_profile",
            "current_profile",
            "encoder",
            "last_error_code",
        }
        changes = {key: value for key, value in values.items() if key in allowed}
        if "supported_profiles" in values:
            changes["supported_profiles_json"] = json.dumps(
                values["supported_profiles"], separators=(",", ":")
            )
        changes["updated_at"] = _now()
        assignments = ", ".join(f"{column} = ?" for column in changes)
        with self.database.transaction() as connection:
            connection.execute(
                f"UPDATE camera_video_profiles SET {assignments} WHERE camera_id = ?",
                (*changes.values(), camera_id),
            )
            if "current_profile" in values:
                connection.execute(
                    """
                    UPDATE camera_runtime_status
                    SET current_video_profile = ?, updated_at = ?
                    WHERE camera_id = ?
                    """,
                    (values["current_profile"], changes["updated_at"], camera_id),
                )
        return self.get_camera_video_profile(camera_id)

    def update_camera_runtime_status(
        self, camera_id: str, values: dict[str, Any]
    ) -> dict[str, Any] | None:
        now = _now()
        seen_at = values.get("last_seen_at") or (now if values["online"] else None)
        with self.database.transaction() as connection:
            camera = connection.execute(
                "SELECT edge_device_id FROM cameras WHERE camera_id = ?", (camera_id,)
            ).fetchone()
            if camera is None:
                return None
            previous = connection.execute(
                "SELECT * FROM camera_runtime_status WHERE camera_id = ?",
                (camera_id,),
            ).fetchone()
            previous_edge = None
            if camera["edge_device_id"] is not None:
                previous_edge = connection.execute(
                    "SELECT * FROM edge_runtime_status WHERE edge_device_id = ?",
                    (camera["edge_device_id"],),
                ).fetchone()
            camera_input = values.get(
                "camera_input",
                previous["camera_input_status"] if previous is not None else "unknown",
            )
            central_status = values.get(
                "central_connection_status",
                previous["central_connection_status"]
                if previous is not None
                else "unknown",
            )
            current_profile = values.get(
                "current_video_profile",
                previous["current_video_profile"] if previous is not None else "hd",
            )
            connection.execute(
                """
                INSERT INTO camera_runtime_status(
                    camera_id, camera_input_status, central_connection_status,
                    current_video_profile, event_cursor, last_seen_at,
                    last_error_code, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(camera_id) DO UPDATE SET
                    camera_input_status = excluded.camera_input_status,
                    central_connection_status = excluded.central_connection_status,
                    current_video_profile = excluded.current_video_profile,
                    event_cursor = COALESCE(excluded.event_cursor,
                                            camera_runtime_status.event_cursor),
                    last_seen_at = COALESCE(excluded.last_seen_at,
                                            camera_runtime_status.last_seen_at),
                    last_error_code = excluded.last_error_code,
                    updated_at = excluded.updated_at
                """,
                (
                    camera_id,
                    camera_input,
                    central_status,
                    current_profile,
                    values.get("event_cursor"),
                    seen_at,
                    values.get("last_error_code"),
                    now,
                ),
            )
            edge_device_id = camera["edge_device_id"]
            if edge_device_id is not None:

                def edge_value(name: str, default: Any = None) -> Any:
                    if name in values:
                        return values[name]
                    if previous_edge is not None:
                        return previous_edge[name]
                    return default

                connection.execute(
                    """
                    INSERT INTO edge_runtime_status(
                        edge_device_id, online, cpu_percent, memory_percent,
                        storage_percent, battery_percent, power_source,
                        last_seen_at, last_error_code, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(edge_device_id) DO UPDATE SET
                        online = excluded.online,
                        cpu_percent = excluded.cpu_percent,
                        memory_percent = excluded.memory_percent,
                        storage_percent = excluded.storage_percent,
                        battery_percent = excluded.battery_percent,
                        power_source = excluded.power_source,
                        last_seen_at = COALESCE(excluded.last_seen_at,
                                              edge_runtime_status.last_seen_at),
                        last_error_code = excluded.last_error_code,
                        updated_at = excluded.updated_at
                    """,
                    (
                        edge_device_id,
                        int(values["online"]),
                        edge_value("cpu_percent"),
                        edge_value("memory_percent"),
                        edge_value("storage_percent"),
                        edge_value("battery_percent"),
                        edge_value("power_source", "unknown"),
                        seen_at,
                        edge_value("last_error_code"),
                        now,
                    ),
                )
            reported_profile = values.get("current_video_profile")
            if reported_profile is not None:
                connection.execute(
                    """
                    UPDATE camera_video_profiles
                    SET current_profile = ?, updated_at = ? WHERE camera_id = ?
                    """,
                    (reported_profile, now, camera_id),
                )
        result = self.get_camera_runtime_status(camera_id)
        if result is not None:
            result["previous_camera_input"] = (
                previous["camera_input_status"] if previous is not None else "unknown"
            )
            result["previous_central_connection_status"] = (
                previous["central_connection_status"]
                if previous is not None
                else "unknown"
            )
            result["previous_online"] = (
                bool(previous_edge["online"]) if previous_edge is not None else None
            )
            result["previous_power_source"] = (
                previous_edge["power_source"]
                if previous_edge is not None
                else "unknown"
            )
            result["previous_battery_percent"] = (
                previous_edge["battery_percent"] if previous_edge is not None else None
            )
            result["previous_storage_percent"] = (
                previous_edge["storage_percent"] if previous_edge is not None else None
            )
        return result

    def get_camera_runtime_status(self, camera_id: str) -> dict[str, Any] | None:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT c.camera_id, COALESCE(e.online, 0) AS online,
                       e.online AS online_observed,
                       e.cpu_percent, e.memory_percent, e.storage_percent,
                       e.battery_percent, COALESCE(e.power_source, 'unknown') AS power_source,
                       r.camera_input_status AS camera_input,
                       r.central_connection_status,
                       r.current_video_profile,
                       r.event_cursor,
                       COALESCE(r.last_seen_at, e.last_seen_at) AS last_seen_at,
                       COALESCE(r.last_error_code, e.last_error_code) AS last_error_code,
                       r.updated_at AS runtime_updated_at
                FROM cameras c
                JOIN camera_runtime_status r ON r.camera_id = c.camera_id
                LEFT JOIN edge_runtime_status e
                       ON e.edge_device_id = c.edge_device_id
                WHERE c.camera_id = ?
                """,
                (camera_id,),
            ).fetchone()
        return _runtime_status(row)

    def delete_camera(self, camera_id: str) -> bool:
        with self.database.transaction() as connection:
            camera = connection.execute(
                "SELECT edge_device_id FROM cameras WHERE camera_id = ?",
                (camera_id,),
            ).fetchone()
            if camera is not None:
                has_history = connection.execute(
                    """
                    SELECT EXISTS(
                        SELECT 1 FROM recording_segments WHERE camera_id = ?
                        UNION ALL
                        SELECT 1 FROM events WHERE camera_id = ?
                        UNION ALL
                        SELECT 1 FROM recovery_jobs WHERE camera_id = ?
                    )
                    """,
                    (camera_id, camera_id, camera_id),
                ).fetchone()[0]
                if has_history:
                    raise CameraHasHistory
            cursor = connection.execute(
                "DELETE FROM cameras WHERE camera_id = ?", (camera_id,)
            )
            if cursor.rowcount > 0 and camera is not None:
                edge_device_id = camera["edge_device_id"]
                if edge_device_id is not None:
                    connection.execute(
                        """
                        DELETE FROM edge_devices
                        WHERE edge_device_id = ?
                          AND NOT EXISTS(
                              SELECT 1 FROM cameras WHERE edge_device_id = ?
                          )
                        """,
                        (edge_device_id, edge_device_id),
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
                    # Finish the transaction before using get_event's separate
                    # connection below.
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
            primary_id = values.get("recording_segment_id")
            if primary_id is None and segment_ids:
                primary_id = min(segment_ids)
            cursor = connection.execute(
                """
                INSERT INTO events(
                    camera_id, event_type, occurred_at, person_id, track_id,
                    confidence, recording_segment_id, snapshot_path,
                    metadata_json, edge_event_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    edge_event_id,
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

    # Central connection outage and recovery state
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
