"""Users persistence and SQL operations."""

from __future__ import annotations

from typing import Any

from ..connection import Database
from .base import _camera, _now, _user


class UsersRepositoryMixin:
    database: Database

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
