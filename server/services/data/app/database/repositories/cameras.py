# 카메라·Edge 정보와 영상 설정을 SQL로 관리하고 삭제 시 관련 이력이 손실되지 않게 검사한다.

from __future__ import annotations

import json
from typing import Any

from ..connection import Database
from .base import (
    CameraHasHistory,
    CameraLimitReached,
    _as_dict,
    _camera,
    _now,
    _runtime_status,
    _video_profile,
)


class CamerasRepositoryMixin:
    database: Database

    def camera_count(self) -> int:
        with self.database.connection() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM cameras").fetchone()[0])

    # 활성 카메라 수 제한을 검사한 트랜잭션 안에서 카메라와 초기 실행 상태·프로필을 생성한다.
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
        """이력을 잃지 않고 카메라를 삭제할 수 있는지 검사한다."""

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

    # 일반 사용자는 권한 테이블로 제한하고 관리자는 전체 카메라를 조회한다.
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

    # 부분 수정과 활성 수 제한을 적용하고 Edge 변경 시 더 이상 참조되지 않는 이전 장치를 정리한다.
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

    # 장치 ID를 기준으로 제어·복구 주소와 인증값을 함께 등록하거나 교체한다.
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

    # 등록된 Edge 연결 정보가 있는 카메라만 제어 대상으로 해석한다.
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

    # 활성 카메라의 Edge 인증 정보와 마지막 이벤트 커서를 상태 수집 작업에 제공한다.
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

    # 저장된 프로필과 Edge 연결 상태를 결합하되 관측되지 않은 연결은 offline으로 표시한다.
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

    # 현재 프로필이 바뀌면 실행 상태의 프로필도 같은 트랜잭션에서 맞춘다.
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

    # 부분 상태 보고에서 빠진 관측값은 유지하고 비교용 이전 상태를 결과에 함께 돌려준다.
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

                # 명시한 None은 새 관측으로 반영하고 키가 없는 경우에만 이전 값을 재사용한다.
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

    # 카메라별 상태와 장치 공통 상태를 합치며 온라인 미관측 여부도 별도 필드로 보존한다.
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

    # 이력 검사와 삭제를 같은 트랜잭션에서 수행해 사전 검사 뒤 새 이력이 생긴 경쟁도 막는다.
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

    # 기존 카메라에만 송출 사용자명·해시를 저장하고 최초 생성 시각은 유지한다.
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
