"""Sessions persistence and SQL operations."""

from __future__ import annotations

from typing import Any

from ..connection import Database
from .base import _as_dict, _now


class SessionsRepositoryMixin:
    database: Database

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
            connection.execute(
                "DELETE FROM mobile_devices WHERE family_id IN "
                "(SELECT COALESCE(family_id,jti) FROM refresh_tokens WHERE jti=?)",
                (jti,),
            )
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
