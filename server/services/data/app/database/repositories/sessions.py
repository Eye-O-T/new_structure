# 토큰의 발급·교체·폐기 이력을 저장하여 이미 로그아웃한 세션의 재사용을 막는다.

from __future__ import annotations

import sqlite3
from typing import Any

from ..connection import Database
from .base import _as_dict, _now


class SessionsRepositoryMixin:
    database: Database

    # 갱신 시 이전 토큰의 만료·폐기·소유자를 확인하고 새 토큰과 교체 이력을 함께 기록한다.
    def issue_refresh_token(self, values: dict[str, Any]) -> dict[str, Any]:
        # 새 토큰 저장과 이전 토큰 폐기를 함께 확정하여 같은 갱신 토큰의 동시 재사용을 막는다.
        now = _now()
        with self.database.transaction() as connection:
            rotated_from = values.get("rotated_from_jti")
            family_id = values.get("family_id") or values["jti"]
            if rotated_from:
                previous = connection.execute(
                    "SELECT * FROM refresh_tokens WHERE jti = ?", (rotated_from,)
                ).fetchone()
                if previous is None:
                    raise LookupError("refresh token to rotate was not found")
                if (
                    previous["revoked_at"] is not None
                    or previous["replaced_by_jti"] is not None
                    or previous["expires_at"] <= now
                ):
                    raise PermissionError("refresh token is revoked or expired")
                if int(previous["user_id"]) != int(values["user_id"]):
                    raise PermissionError("refresh token owner does not match")
                family_id = previous["family_id"] or previous["jti"]
                if values.get("family_id") not in {None, family_id}:
                    raise PermissionError("refresh token family cannot change")
            elif connection.execute(
                "SELECT 1 FROM refresh_tokens WHERE user_id=? "
                "AND COALESCE(family_id,jti)=? LIMIT 1",
                (values["user_id"], family_id),
            ).fetchone() is not None:
                # 새 로그인은 새 family를 사용한다. 폐기된 계열을 초기 발급으로 되살리지 않는다.
                raise PermissionError("refresh token family already exists")
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
                    family_id,
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

    @staticmethod
    def _revoke_family(
        connection: sqlite3.Connection, user_id: int, family_id: str, now: str
    ) -> None:
        connection.execute(
            "UPDATE refresh_tokens SET revoked_at=COALESCE(revoked_at,?) "
            "WHERE user_id=? AND COALESCE(family_id,jti)=?",
            (now, user_id, family_id),
        )
        connection.execute(
            "DELETE FROM mobile_devices WHERE user_id=? AND family_id=?",
            (user_id, family_id),
        )

    # 이전 refresh로 로그아웃해도 이미 회전된 후속 토큰까지 같은 트랜잭션에서 폐기한다.
    def delete_refresh_token(self, jti: str) -> bool:
        with self.database.transaction() as connection:
            token = connection.execute(
                "SELECT user_id,COALESCE(family_id,jti) AS family FROM refresh_tokens WHERE jti=?",
                (jti,),
            ).fetchone()
            if token is None:
                return False
            # 행은 만료 후 보관 정리까지 남긴다. 지연된 로그아웃·회전 요청도 폐기를 관측한다.
            self._revoke_family(connection, token["user_id"], token["family"], _now())
            return True

    def revoke_token_family(self, user_id: int, family_id: str) -> None:
        with self.database.transaction() as connection:
            self._revoke_family(connection, user_id, family_id, _now())

    def token_family_is_active(self, user_id: int, family_id: str) -> bool:
        with self.database.connection() as connection:
            return connection.execute(
                "SELECT 1 FROM refresh_tokens r JOIN users u ON u.id=r.user_id "
                "WHERE r.user_id=? AND COALESCE(r.family_id,r.jti)=? AND u.is_active=1 "
                "AND r.revoked_at IS NULL AND r.replaced_by_jti IS NULL AND r.expires_at>? LIMIT 1",
                (user_id, family_id, _now()),
            ).fetchone() is not None

    # 같은 jti의 재폐기 요청은 새 행 대신 폐기 정보와 만료 시각을 갱신한다.
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
