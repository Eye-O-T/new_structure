# 실제 Data 저장소를 사용해 푸시 권한·세션 연결·작업 임대와 외부 API 인증을 검증한다.
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from ai_cctv_core.time import format_utc, utc_now
from server.services.data.app.config import Settings as DataSettings
from server.services.data.app.database.connection import Database
from server.services.data.app.main import create_app as create_data_app
from server.services.data.app.database.repositories import DataRepository
from server.services.external.app.config import Settings
from server.services.external.app.clients.data import DataClient
from server.services.external.app.main import create_app
from server.services.external.app.notifications.firebase import (
    FirebaseSender,
    PushSendError,
)
from server.services.external.app.workers.push_dispatcher import PushDispatcher
from server.services.external.app.security.passwords import hash_password
from server.setup.install_helper.compose_adapter import ComposeAdapter


# 관리자·허용된 조회자·미허용 조회자를 같은 DB에 준비해 수신 권한 차이를 비교한다.
@pytest.fixture
def repository(tmp_path: Path):
    repo = DataRepository(Database(tmp_path / "db.sqlite"))
    repo.initialize()
    for name, role in [("admin", "admin"), ("viewer", "viewer"), ("other", "viewer")]:
        user = repo.create_user(
            {
                "username": name,
                "role": role,
                "password_hash": hash_password("test-password"),
            }
        )
        repo.issue_refresh_token(
            {
                "user_id": user["id"],
                "jti": name,
                "family_id": name,
                "token_hash": "hash",
                "expires_at": format_utc(utc_now() + timedelta(days=1)),
            }
        )
    for camera in ["cam-001", "cam-002"]:
        repo.create_camera({"camera_id": camera, "name": camera, "stream_path": camera})
    repo.grant_camera(2, "cam-001")
    return repo


# 사용자의 refresh 세션에 기기를 연결하고 이벤트 필터·FCM 토큰을 시나리오별로 바꾼다.
def register(repo, user=1, event_types=None, token=None):
    device = f"{user:032x}"
    repo.put_mobile_device(
        {
            "device_id": device,
            "user_id": user,
            "refresh_jti": {1: "admin", 2: "viewer", 3: "other"}[user],
            "token": token or f"token-{user}-" + "x" * 30,
            "platform": "android",
            "enabled": True,
            "event_types": event_types,
        }
    )
    return device


# 현재 시각의 이벤트를 생성해 만료되지 않은 전송 작업이 준비되도록 한다.
def event(repo, camera="cam-001", edge_id=None, event_type="person_appeared"):
    return repo.create_event(
        {
            "camera_id": camera,
            "event_type": event_type,
            "occurred_at": format_utc(utc_now()),
            "edge_event_id": edge_id,
        }
    )


# 권한과 수신 필터를 모두 만족한 기기에만 작업을 만들며 같은 Edge 이벤트는 중복 발송하지 않는다.
def test_enqueue_acl_preferences_and_idempotence(repository):
    repo = repository
    register(repo, 1)
    register(repo, 2, ["person_appeared"])
    register(repo, 3)
    first = event(repo, edge_id="edge-event-1")
    assert event(repo, edge_id="edge-event-1")["id"] == first["id"]
    event(repo, "cam-002")
    event(repo, event_type="edge_online")
    deliveries = []
    while delivery := repo.claim_push():
        deliveries.append(delivery)
        assert repo.complete_push(delivery["id"], delivery["lease_id"], "sent", None)
    assert [d["user_id"] for d in deliveries].count(1) == 3
    assert [d["user_id"] for d in deliveries].count(2) == 1
    assert not any(d["user_id"] == 3 for d in deliveries)


# 이벤트 생성 후 권한·역할·세션·기기 상태가 바뀌면 발송 직전 다시 검사해야 한다.
@pytest.mark.parametrize("revoke", ["acl", "inactive", "role", "session", "disabled"])
def test_revalidate_before_send(repository, revoke):
    repo = repository
    device = register(repo, 2)
    event(repo)
    if revoke == "acl":
        repo.revoke_camera(2, "cam-001")
    elif revoke == "inactive":
        repo.update_user(2, {"is_active": False})
    elif revoke == "role":
        repo.update_user(2, {"role": "admin"})
    elif revoke == "session":
        repo.delete_refresh_token("viewer")
    else:
        with repo.database.transaction() as conn:
            conn.execute(
                "UPDATE mobile_devices SET enabled=0 WHERE device_id=?", (device,)
            )
    assert repo.claim_push() is None


# 정상 토큰 회전은 기기 연결을 이어가고 로그아웃은 연결과 이후 수신을 제거한다.
def test_session_rotation_preserves_push_and_logout_removes_it(repository):
    repo = repository
    register(repo)
    repo.issue_refresh_token(
        {
            "user_id": 1,
            "jti": "rotated",
            "family_id": "admin",
            "token_hash": "new-hash",
            "rotated_from_jti": "admin",
            "expires_at": format_utc(utc_now() + timedelta(days=1)),
        }
    )
    event(repo)
    assert repo.claim_push() is not None
    repo.delete_refresh_token("rotated")
    event(repo)
    assert repo.claim_push() is None
    with repo.database.connection() as conn:
        assert conn.execute("SELECT count(*) FROM mobile_devices").fetchone()[0] == 0


# 재시작한 두 작업자 중 하나만 임대하고 만료 후 재임대한 작업은 이전 완료 응답을 거부한다.
def test_restart_concurrent_claim_retry_and_stale_ack(repository):
    repo = repository
    register(repo)
    event(repo)
    restarted = DataRepository(Database(repo.database.path))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: restarted.claim_push(), range(2)))
    assert sum(d is not None for d in results) == 1
    original = next(d for d in results if d)
    with repo.database.transaction() as conn:
        conn.execute("UPDATE push_deliveries SET lease_until='2000-01-01T00:00:00Z'")
    recovered = restarted.claim_push()
    assert recovered["attempt_count"] == 2
    assert recovered["lease_id"] != original["lease_id"]
    assert not repo.complete_push(original["id"], original["lease_id"], "sent", None)
    assert repo.complete_push(
        recovered["id"], recovered["lease_id"], "retry", "FCM_TIMEOUT"
    )
    assert repo.claim_push() is None


# 전송 유효기간이 지났거나 FCM이 폐기한 토큰이면 후속 발송도 멈춰야 한다.
def test_expired_delivery_and_invalid_token_cleanup(repository):
    repo = repository
    register(repo)
    event(repo)
    with repo.database.transaction() as conn:
        conn.execute("UPDATE push_deliveries SET expires_at='2000-01-01T00:00:00Z'")
    assert repo.claim_push() is None
    event(repo)
    delivery = repo.claim_push()
    repo.complete_push(
        delivery["id"], delivery["lease_id"], "invalid_token", "UNREGISTERED"
    )
    event(repo)
    assert repo.claim_push() is None


# 같은 단말 토큰을 다른 계정에 연결할 때 이전 계정의 대기 알림을 취소한다.
def test_rebinding_token_cancels_previous_account_deliveries(repository):
    repo = repository
    token = "shared-device-token-" + "x" * 30
    register(repo, 1, token=token)
    event(repo, "cam-002")
    register(repo, 2, token=token)
    assert repo.claim_push() is None
    event(repo)
    assert repo.claim_push()["user_id"] == 2


# 실제 배포 환경변수 없이 외부 API와 발송기의 필수 설정을 제공한다.
def settings():
    return Settings(
        data_base_url="http://data/internal/v1",
        data_health_url="http://data/health/ready",
        internal_token="t" * 32,
        jwt_secret="j" * 32,
        media_read_username="reader",
        media_read_password="m" * 32,
    )


# push.env가 있을 때만 푸시 overlay를 추가하고 기존 compose 환경 파일은 계속 사용한다.
def test_install_helper_push_overlay_is_opt_in_and_preserves_base(tmp_path):
    server = tmp_path / "server"
    server.mkdir()
    env = tmp_path / "configuration" / "compose.env"
    env.parent.mkdir()
    env.write_text("PUBLIC_BASE_URL=https://cctv.test\n", encoding="utf-8")
    adapter = ComposeAdapter(server, env)
    plain = adapter.command("up", "-d")
    assert "compose.push.yml" not in " ".join(plain)
    env.with_name("push.env").write_text("PUSH_ENABLED=true\n", encoding="utf-8")
    command = adapter.command("up", "-d")
    assert str(env) in command
    assert str(env.with_name("push.env")) in command
    assert str(server / "compose.push.yml") in command
    assert command[-2:] == ["up", "-d"]


# ASGI로 실제 인증 API와 Data API를 연결해 다른 계정의 refresh 토큰·기기 접근을 거부한다.
@pytest.mark.asyncio
async def test_public_registration_real_auth_and_data_api(tmp_path):
    data_app = create_data_app(
        settings=DataSettings(
            database_path=tmp_path / "db.sqlite",
            storage_root=tmp_path / "recordings",
            snapshot_root=tmp_path / "snapshots",
            backup_root=tmp_path / "backups",
            internal_token="t" * 32,
        )
    )
    with TestClient(data_app):
        repo = data_app.state.repository
        for name in ["admin", "other"]:
            repo.create_user(
                {
                    "username": name,
                    "role": "admin",
                    "password_hash": hash_password("test-password"),
                }
            )
        data = DataClient(
            base_url="http://data/internal/v1",
            health_url="http://data/health/ready",
            internal_token="t" * 32,
            transport=httpx.ASGITransport(app=data_app),
        )
        app = create_app(settings=settings(), data_client=data)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://cctv.test"
        ) as client:
            assert (
                await client.put("/api/v1/notifications/devices", json={})
            ).status_code == 401
            login = (
                await client.post(
                    "/api/v1/auth/login",
                    json={"username": "admin", "password": "test-password"},
                )
            ).json()
            headers = {"Authorization": "Bearer " + login["access_token"]}
            body = {
                "device_id": "a" * 32,
                "token": "secret-fcm-token-" + "x" * 30,
                "platform": "android",
                "enabled": True,
                "event_types": None,
                "refresh_token": login["refresh_token"],
            }
            response = await client.put(
                "/api/v1/notifications/devices", headers=headers, json=body
            )
            assert response.status_code == 200, response.text
            assert "token" not in response.json()
            other = (
                await client.post(
                    "/api/v1/auth/login",
                    json={"username": "other", "password": "test-password"},
                )
            ).json()
            forged = await client.put(
                "/api/v1/notifications/devices",
                headers=headers,
                json={**body, "refresh_token": other["refresh_token"]},
            )
            assert forged.status_code == 401
            assert (
                await client.delete(
                    "/api/v1/notifications/devices/" + "a" * 32,
                    headers={"Authorization": "Bearer " + other["access_token"]},
                )
            ).status_code == 204
            with repo.database.connection() as conn:
                assert (
                    conn.execute("SELECT count(*) FROM mobile_devices").fetchone()[0]
                    == 1
                )
            assert (
                await client.post(
                    "/api/v1/auth/logout",
                    headers=headers,
                    json={"refresh_token": login["refresh_token"]},
                )
            ).status_code == 204
            with repo.database.connection() as conn:
                assert (
                    conn.execute("SELECT count(*) FROM mobile_devices").fetchone()[0]
                    == 0
                )
        await data.close()


# 발송기의 성공·일시 실패·무효 토큰 결과가 작업 완료 상태로 그대로 전달되어야 한다.
@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["sent", "retry", "invalid_token"])
async def test_dispatcher_acknowledges_sender_outcome(outcome):
    class FakeData:
        result = None

        async def claim_push(self):
            return {"id": 1, "lease_id": "lease"}

        async def complete_push(self, delivery, result, error_code=None):
            self.result = result

    class Sender:
        async def send(self, delivery):
            if outcome != "sent":
                raise PushSendError(outcome, "TEST_FAILURE")

    data = FakeData()
    assert await PushDispatcher(settings(), data, Sender()).dispatch_once()
    assert data.result == outcome


# Firebase 메시지 생성은 실제 SDK를 쓰고 전송만 대체해 개인정보 노출과 오류 변환을 검사한다.
def test_real_firebase_message_and_unregistered_error_without_network(monkeypatch):
    from firebase_admin import messaging

    sender = FirebaseSender(settings())
    sender._app = object()
    captured = []

    def capture(message, app):
        captured.append(message)
        return "fake-message-id"

    monkeypatch.setattr(messaging, "send", capture)
    delivery = {
        "event_id": 42,
        "user_id": 1,
        "device_id": "a" * 32,
        "camera_id": "cam-001",
        "occurred_at": "2026-09-06T00:00:00Z",
        "token": "private-device-token",
    }
    sender._send(delivery)
    message = captured[0]
    assert message.data["event_id"] == "42"
    assert message.android.notification.channel_id == "cctv_events"
    assert message.android.notification.tag == "event-42"
    assert "token" not in message.data
    assert "cam-001" not in message.notification.body

    def unregistered(message, app):
        raise messaging.UnregisteredError("sensitive upstream details")

    monkeypatch.setattr(messaging, "send", unregistered)
    with pytest.raises(PushSendError) as error:
        sender._send(delivery)
    assert error.value.outcome == "invalid_token"
    assert str(error.value) == "UNREGISTERED"
