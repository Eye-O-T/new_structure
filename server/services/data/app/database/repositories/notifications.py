# 발송할 알림을 DB에 보관하고 수신 자격 확인과 재시도 일정을 관리한다.
"""FCM 통신은 DB 트랜잭션 밖에서 수행한다. 재전송될 수 있으므로
이벤트·기기 고유 키와 앱의 알림 ID로 일반적인 중복을 억제한다."""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import timedelta
from typing import Any

from ai_cctv_core.time import format_utc, utc_now

from ..connection import Database


class PushRepositoryMixin:
    database: Database

    @staticmethod
    def _eligible_sql() -> str:
        # 등록 당시 권한만 믿지 않고 현재 로그인·역할·카메라 권한·알림 설정을 다시 확인한다.
        return """
            d.enabled = 1 AND u.is_active = 1 AND u.role = d.role
            AND EXISTS (
                SELECT 1 FROM refresh_tokens r WHERE r.user_id = d.user_id
                AND COALESCE(r.family_id, r.jti) = d.family_id
                AND r.revoked_at IS NULL AND r.replaced_by_jti IS NULL
                AND r.expires_at > :now
            )
            AND (u.role = 'admin' OR EXISTS (
                SELECT 1 FROM user_camera_permissions p
                JOIN cameras c ON c.id = p.camera_id
                WHERE p.user_id = d.user_id AND c.camera_id = e.camera_id
            ))
            AND (d.event_types_json IS NULL OR EXISTS (
                SELECT 1 FROM json_each(d.event_types_json)
                WHERE value = e.event_type
            ))
        """

    def put_mobile_device(self, values: dict[str, Any]) -> dict[str, Any]:
        now = format_utc(utc_now())
        with self.database.transaction() as connection:
            session = connection.execute(
                "SELECT r.*, u.role, u.is_active FROM refresh_tokens r "
                "JOIN users u ON u.id = r.user_id WHERE r.jti = ?",
                (values["refresh_jti"],),
            ).fetchone()
            if (
                session is None
                or session["user_id"] != int(values["user_id"])
                or not session["is_active"]
                or session["revoked_at"] is not None
                or session["replaced_by_jti"] is not None
                or session["expires_at"] <= now
            ):
                raise PermissionError("Session is unavailable")
            family = session["family_id"] or session["jti"]
            # 단말이 다른 계정으로 바뀌면 이전 발송 예약을 취소해 이전 계정의 이벤트 노출을 막는다.
            connection.execute(
                "DELETE FROM mobile_devices WHERE token = ? AND device_id != ?",
                (values["token"], values["device_id"]),
            )
            old = connection.execute(
                "SELECT * FROM mobile_devices WHERE device_id = ?",
                (values["device_id"],),
            ).fetchone()
            if old and (
                old["user_id"] != int(values["user_id"])
                or old["family_id"] != family
                or old["token"] != values["token"]
            ):
                connection.execute(
                    "DELETE FROM push_deliveries WHERE device_id = ?",
                    (values["device_id"],),
                )
            connection.execute(
                """INSERT INTO mobile_devices
                (device_id,user_id,family_id,role,token,platform,enabled,
                 event_types_json,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(device_id) DO UPDATE SET
                user_id=excluded.user_id, family_id=excluded.family_id,
                role=excluded.role, token=excluded.token, platform=excluded.platform,
                enabled=excluded.enabled, event_types_json=excluded.event_types_json,
                updated_at=excluded.updated_at""",
                (
                    values["device_id"],
                    values["user_id"],
                    family,
                    session["role"],
                    values["token"],
                    values["platform"],
                    int(values["enabled"]),
                    json.dumps(values["event_types"])
                    if values["event_types"] is not None
                    else None,
                    now,
                    now,
                ),
            )
        return {
            "device_id": values["device_id"],
            "enabled": values["enabled"],
            "event_types": values["event_types"],
            "platform": values["platform"],
        }

    def delete_mobile_device(self, device_id: str, user_id: int) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM mobile_devices WHERE device_id = ? AND user_id = ?",
                (device_id, user_id),
            )

    def _enqueue_push(self, connection: sqlite3.Connection, event_id: int) -> None:
        now = utc_now()
        connection.execute(
            """INSERT OR IGNORE INTO push_deliveries
            (event_id,device_id,next_attempt_at,expires_at,created_at,updated_at)
            SELECT e.id,d.device_id,:now,:expiry,:now,:now
            FROM events e CROSS JOIN mobile_devices d JOIN users u ON u.id=d.user_id
            WHERE e.id=:event_id AND """
            + self._eligible_sql(),
            {
                "event_id": event_id,
                "now": format_utc(now),
                "expiry": format_utc(now + timedelta(hours=1)),
            },
        )

    def claim_push(self) -> dict[str, Any] | None:
        # 발송 직전에 수신 자격을 재검사하고 2분 동안 처리 권한을 임대한다.
        # 실제 FCM 통신은 이 트랜잭션이 끝난 뒤 External에서 수행하여 DB 잠금을 오래 잡지 않는다.
        now = utc_now()
        params = {"now": format_utc(now)}
        with self.database.transaction() as connection:
            connection.execute(
                "DELETE FROM push_deliveries WHERE updated_at < ? AND "
                "state IN ('sent','failed','cancelled')",
                (format_utc(now - timedelta(days=7)),),
            )
            connection.execute(
                "UPDATE push_deliveries SET state='cancelled',updated_at=:now "
                "WHERE state IN ('pending','sending') AND (expires_at <= :now OR "
                "NOT EXISTS (SELECT 1 FROM mobile_devices d JOIN users u "
                "ON u.id=d.user_id JOIN events e ON e.id=push_deliveries.event_id "
                "WHERE d.device_id=push_deliveries.device_id AND "
                + self._eligible_sql()
                + "))",
                params,
            )
            connection.execute(
                "UPDATE push_deliveries SET state='failed',last_error_code="
                "'ATTEMPTS_EXHAUSTED',updated_at=:now WHERE attempt_count >= 8 "
                "AND (state='pending' OR (state='sending' AND lease_until<=:now))",
                params,
            )
            row = connection.execute(
                """SELECT p.id,p.event_id,p.device_id,p.attempt_count,d.token,
                d.user_id,e.camera_id,e.event_type,e.occurred_at
                FROM push_deliveries p JOIN mobile_devices d ON d.device_id=p.device_id
                JOIN events e ON e.id=p.event_id
                WHERE p.attempt_count < 8 AND
                ((p.state='pending' AND p.next_attempt_at<=:now) OR
                 (p.state='sending' AND p.lease_until<=:now))
                ORDER BY p.id LIMIT 1""",
                params,
            ).fetchone()
            if row is None:
                return None
            lease = uuid.uuid4().hex
            connection.execute(
                "UPDATE push_deliveries SET state='sending',lease_id=?,lease_until=?,"
                "attempt_count=attempt_count+1,updated_at=? WHERE id=?",
                (
                    lease,
                    format_utc(now + timedelta(minutes=2)),
                    params["now"],
                    row["id"],
                ),
            )
            return {
                **dict(row),
                "lease_id": lease,
                "attempt_count": row["attempt_count"] + 1,
            }

    def complete_push(
        self, delivery_id: int, lease_id: str, outcome: str, error_code: str | None
    ) -> bool:
        # 작업이 재할당되면 임대 ID가 바뀌므로 이전 발송자의 늦은 완료 보고를 무시할 수 있다.
        now = utc_now()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM push_deliveries WHERE id=? AND lease_id=? "
                "AND state='sending'",
                (delivery_id, lease_id),
            ).fetchone()
            if row is None:
                return False
            if outcome == "invalid_token":
                connection.execute(
                    "DELETE FROM mobile_devices WHERE device_id=?", (row["device_id"],)
                )
                return True
            state = (
                "sent"
                if outcome == "sent"
                else "failed"
                if outcome == "permanent_failure" or row["attempt_count"] >= 8
                else "pending"
            )
            # 장애 동안 요청이 몰리지 않도록 재시도 간격을 두 배씩 늘리되 최대 15분으로 제한한다.
            delay = min(900, 30 * 2 ** (row["attempt_count"] - 1))
            connection.execute(
                "UPDATE push_deliveries SET state=?,next_attempt_at=?,lease_id=NULL,"
                "lease_until=NULL,last_error_code=?,updated_at=? WHERE id=?",
                (
                    state,
                    format_utc(now + timedelta(seconds=delay)),
                    error_code,
                    format_utc(now),
                    delivery_id,
                ),
            )
            return True
