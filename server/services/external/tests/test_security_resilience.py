"""Session revocation, proxy address trust, bounded authentication state and input errors."""

import asyncio
import hashlib
from dataclasses import replace

import httpx
import jwt
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from server.services.data.app.config import Settings as DataSettings
from server.services.data.app.main import create_app as create_data_app
from server.services.external.app.clients.data import DataClient
from server.services.external.app.main import create_app
from server.services.external.app.security.client_address import TrustedProxyAddresses
from server.services.external.app.security.login_backoff import LoginBackoff
from server.services.external.app.security.tokens import issue_token, utc_iso_from_epoch
from server.services.external.app.workers.status_collector import StatusCollector
from server.services.external.tests import test_external_service as support
from server.services.external.tests.test_external_service import FakeDataClient


@pytest.fixture(scope="module")
def password_hash():
    return support.password_hash.__wrapped__()


@pytest.fixture
def settings():
    return support.settings.__wrapped__()


async def login(client):
    response = await client.post(
        "/api/v1/auth/login",
        json={
            "username": "admin",
            "password": "correct horse battery staple",
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.asyncio
async def test_logout_old_pair_revokes_rotated_access_and_only_its_family(
    settings, password_hash
):
    data = FakeDataClient(password_hash)
    app = create_app(settings=settings, data_client=data)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        original = await login(client)
        independent = await login(client)
        rotated = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": original["refresh_token"]}
        )
        assert rotated.status_code == 200
        newer = rotated.json()
        old_sid = jwt.decode(
            original["access_token"], options={"verify_signature": False}
        )["sid"]
        assert (
            jwt.decode(newer["access_token"], options={"verify_signature": False})[
                "sid"
            ]
            == old_sid
        )
        assert (
            jwt.decode(newer["refresh_token"], options={"verify_signature": False})[
                "sid"
            ]
            == old_sid
        )
        assert (
            await client.get(
                "/api/v1/cameras",
                headers={"Authorization": f"Bearer {original['access_token']}"},
            )
        ).status_code == 200
        logout = await client.post(
            "/api/v1/auth/logout",
            headers={"Authorization": f"Bearer {original['access_token']}"},
            json={"refresh_token": original["refresh_token"]},
        )
        assert logout.status_code == 204
        for access in (original["access_token"], newer["access_token"]):
            assert (
                await client.get(
                    "/api/v1/cameras", headers={"Authorization": f"Bearer {access}"}
                )
            ).status_code == 401
        assert (
            await client.post(
                "/api/v1/auth/refresh", json={"refresh_token": newer["refresh_token"]}
            )
        ).status_code == 401
        assert (
            await client.get(
                "/api/v1/cameras",
                headers={"Authorization": f"Bearer {independent['access_token']}"},
            )
        ).status_code == 200


@pytest.mark.asyncio
async def test_delayed_refresh_response_after_bearer_only_logout_is_unusable(
    settings, password_hash
):
    data = FakeDataClient(password_hash)
    app = create_app(settings=settings, data_client=data)
    committed = asyncio.Event()
    release = asyncio.Event()
    original_rotate = data.rotate_refresh_token

    async def delayed_rotate(jti, payload):
        result = await original_rotate(jti, payload)
        committed.set()
        await release.wait()
        return result

    data.rotate_refresh_token = delayed_rotate
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        original = await login(client)
        client.cookies.clear()
        pending = asyncio.create_task(
            client.post(
                "/api/v1/auth/refresh",
                json={"refresh_token": original["refresh_token"]},
            )
        )
        await asyncio.wait_for(committed.wait(), 1)
        try:
            logout = await client.post(
                "/api/v1/auth/logout",
                headers={"Authorization": f"Bearer {original['access_token']}"},
            )
            assert logout.status_code == 204
        finally:
            release.set()
        delayed = await pending
        assert delayed.status_code == 200
        result = delayed.json()
        assert (
            await client.get(
                "/api/v1/cameras",
                headers={"Authorization": f"Bearer {result['access_token']}"},
            )
        ).status_code == 401
        assert (
            await client.post(
                "/api/v1/auth/refresh", json={"refresh_token": result["refresh_token"]}
            )
        ).status_code == 401


@pytest.mark.asyncio
async def test_unbound_legacy_access_requires_reauthentication(settings, password_hash):
    app = create_app(settings=settings, data_client=FakeDataClient(password_hash))
    legacy = issue_token(
        settings, user_id="1", role="admin", token_type="access", ttl_seconds=60
    ).encoded
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        assert (
            await client.get(
                "/api/v1/cameras", headers={"Authorization": f"Bearer {legacy}"}
            )
        ).status_code == 401


@pytest.mark.asyncio
async def test_valid_legacy_refresh_migrates_to_bound_access(settings, password_hash):
    data = FakeDataClient(password_hash)
    legacy = issue_token(
        settings, user_id="1", role="admin", token_type="refresh", ttl_seconds=60
    )
    await data.create_refresh_token(
        {
            "jti": legacy.claims.jti,
            "user_id": "1",
            "family_id": legacy.claims.jti,
            "expires_at": utc_iso_from_epoch(legacy.claims.exp),
            "token_hash": hashlib.sha256(legacy.encoded.encode()).hexdigest(),
        }
    )
    app = create_app(settings=settings, data_client=data)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/api/v1/auth/refresh", json={"refresh_token": legacy.encoded}
        )
        assert response.status_code == 200
        token = response.json()["access_token"]
        assert (
            jwt.decode(token, options={"verify_signature": False})["sid"]
            == legacy.claims.jti
        )
        assert (
            await client.get(
                "/api/v1/cameras", headers={"Authorization": f"Bearer {token}"}
            )
        ).status_code == 200


@pytest.mark.asyncio
async def test_verified_nginx_peer_keeps_separate_clients_and_ignores_spoofed_chain(
    settings, password_hash
):
    settings = replace(settings, trusted_proxy_hosts=("172.18.0.5",))
    app = create_app(settings=settings, data_client=FakeDataClient(password_hash))
    transport = httpx.ASGITransport(app=app, client=("172.18.0.5", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/api/v1/auth/login",
            headers={"X-Real-IP": "198.51.100.1"},
            json={"username": "admin", "password": "incorrect"},
        )
        second = await client.post(
            "/api/v1/auth/login",
            headers={
                "X-Real-IP": "203.0.113.2",
                "X-Forwarded-For": "198.51.100.1, 203.0.113.2",
            },
            json={"username": "admin", "password": "correct horse battery staple"},
        )
        assert first.status_code == 401
        assert second.status_code == 200


@pytest.mark.asyncio
async def test_untrusted_peer_cannot_rotate_backoff_identity_with_headers(
    settings, password_hash
):
    settings = replace(settings, trusted_proxy_hosts=("172.18.0.5",))
    app = create_app(settings=settings, data_client=FakeDataClient(password_hash))
    transport = httpx.ASGITransport(app=app, client=("198.51.100.1", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post(
            "/api/v1/auth/login",
            headers={"X-Real-IP": "203.0.113.2"},
            json={"username": "admin", "password": "incorrect"},
        )
        second = await client.post(
            "/api/v1/auth/login",
            headers={"X-Real-IP": "203.0.113.3"},
            json={"username": "admin", "password": "correct horse battery staple"},
        )
        assert first.status_code == 401
        assert second.status_code == 429


def test_login_history_is_bounded_expires_and_keeps_recent_backoff(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(
        "server.services.external.app.security.login_backoff.time.monotonic",
        lambda: clock[0],
    )
    backoff = LoginBackoff(1, 60, max_entries=20, idle_seconds=120)
    for index in range(1000):
        backoff.record_failure(f"ip:unknown-{index}")
    assert len(backoff._attempts) == 20
    clock[0] = 110
    assert backoff.record_failure("ip:unknown-999") == 2
    clock[0] = 121
    assert backoff.retry_after("ip:unknown-0") == 0
    assert len(backoff._attempts) == 1
    assert backoff.record_failure("ip:unknown-999") == 4
    clock[0] = 250
    assert backoff.retry_after("ip:unknown-999") == 0
    assert not backoff._attempts


@pytest.mark.asyncio
async def test_address_failure_burst_limits_changed_names_without_blocking_other_clients(
    settings,
    password_hash,
    monkeypatch,
):
    clock = [100.0]
    monkeypatch.setattr(
        "server.services.external.app.security.login_backoff.time.monotonic",
        lambda: clock[0],
    )
    settings = replace(settings, trusted_proxy_hosts=("172.18.0.5",))
    app = create_app(settings=settings, data_client=FakeDataClient(password_hash))
    transport = httpx.ASGITransport(app=app, client=("172.18.0.5", 50000))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        headers = {"X-Real-IP": "198.51.100.1"}
        for index in range(20):
            response = await client.post(
                "/api/v1/auth/login",
                headers=headers,
                json={
                    "username": f"unknown-{index}",
                    "password": "incorrect",
                },
            )
            assert response.status_code == 401
            if index == 0:
                # 첫 오입력은 다른 계정의 정상 로그인을 막지 않고, 성공도 주소 집계를 없애지 않는다.
                success = await client.post(
                    "/api/v1/auth/login",
                    headers=headers,
                    json={
                        "username": "admin",
                        "password": "correct horse battery staple",
                    },
                )
                assert success.status_code == 200
        credentials = {"username": "admin", "password": "correct horse battery staple"}
        blocked = await client.post(
            "/api/v1/auth/login", headers=headers, json=credentials
        )
        assert blocked.status_code == 429
        assert blocked.headers["retry-after"] == "60"
        allowed = await client.post(
            "/api/v1/auth/login", headers={"X-Real-IP": "203.0.113.2"}, json=credentials
        )
        assert allowed.status_code == 200
        clock[0] = 159.5
        blocked = await client.post(
            "/api/v1/auth/login", headers=headers, json=credentials
        )
        assert blocked.status_code == 429
        assert blocked.headers["retry-after"] == "1"
        clock[0] = 160
        assert (
            await client.post("/api/v1/auth/login", headers=headers, json=credentials)
        ).status_code == 200


def test_address_failure_history_is_bounded_and_expires(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(
        "server.services.external.app.security.login_backoff.time.monotonic",
        lambda: clock[0],
    )
    backoff = LoginBackoff(1, 60, max_entries=3)
    for address in range(100):
        for attempt in range(30):
            backoff.record_failure(f"{address}/unknown-{attempt}", address=str(address))
    assert len(backoff._addresses) == 3
    assert all(len(failures) == 20 for failures in backoff._addresses.values())
    assert backoff.retry_after("new-account", address="99") == 60
    clock[0] = 60
    assert backoff.retry_after("new-account", address="99") == 0
    assert not backoff._addresses


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["/api/v1/events", "/api/v1/recordings"])
@pytest.mark.parametrize(
    "start,end",
    [
        ("2026-01-01T00:00:00", "2026-01-02T00:00:00Z"),
        ("2026-01-01T00:00:00Z", "2026-01-02T00:00:00"),
    ],
)
async def test_mixed_timezones_are_http_input_errors(
    settings, password_hash, path, start, end
):
    app = create_app(settings=settings, data_client=FakeDataClient(password_hash))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        await login(client)
        response = await client.get(
            path, params={"camera_id": "cam-001", "from": start, "to": end}
        )
    assert response.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "capture_state, expected", [("stale", 1), ("error", 1), ("stopped", 0)]
)
async def test_collector_reports_unexpected_capture_loss_once(
    settings, capture_state, expected
):
    data = FakeDataClient("unused")

    class Edge:
        async def get_status(self):
            return {
                "camera_id": "cam-001",
                "online": True,
                "current_profile": "hd",
                "camera_input": "offline",
                "capture_state": capture_state,
                "central_connection_status": "unknown",
            }

        async def list_events(self, **kwargs):
            return {"items": [], "next_cursor": None}

        async def close(self):
            pass

    collector = StatusCollector(
        settings=settings, data_client=data, edge_client_factory=lambda target: Edge()
    )
    target = {"camera_id": "cam-001", "edge_device_id": "edge-001"}
    await collector._collect_target(target)
    await collector._collect_target(target)
    lost = [
        event
        for event in data.edge_events
        if event["event_type"] == "camera_input_lost"
    ]
    assert len(lost) == expected
    assert data.runtime_status["online"] is True
    assert data.runtime_status["camera_input"] == "offline"


@pytest.mark.asyncio
async def test_proxy_dns_change_and_failure_do_not_keep_old_addresses_trusted(
    monkeypatch,
):
    addresses = ["172.18.0.5"]

    async def lookup(host, port, **kwargs):
        assert host == "nginx"
        if not addresses:
            raise OSError("DNS unavailable")
        return [(2, 1, 6, "", (address, 0)) for address in addresses]

    monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", lookup)
    resolver = TrustedProxyAddresses(("nginx",), cache_seconds=0)

    def request(peer):
        return Request(
            {
                "type": "http",
                "client": (peer, 1234),
                "headers": [(b"x-real-ip", b"198.51.100.1")],
            }
        )

    assert await resolver.client_address(request("172.18.0.5")) == "198.51.100.1"
    addresses[:] = ["172.18.0.8"]
    assert await resolver.client_address(request("172.18.0.5")) == "172.18.0.5"
    assert await resolver.client_address(request("172.18.0.8")) == "198.51.100.1"
    addresses.clear()
    assert await resolver.client_address(request("172.18.0.8")) == "172.18.0.8"


@pytest.mark.asyncio
@pytest.mark.parametrize("rotation_order", ["before_logout", "after_logout"])
@pytest.mark.parametrize("logout_credential", ["access", "refresh"])
async def test_real_data_logout_blocks_both_refresh_race_orders(
    tmp_path, settings, password_hash, rotation_order, logout_credential
):
    data_app = create_data_app(
        settings=DataSettings(
            database_path=tmp_path / "db.sqlite",
            storage_root=tmp_path / "recordings",
            snapshot_root=tmp_path / "snapshots",
            backup_root=tmp_path / "backups",
            internal_token=settings.internal_token,
        )
    )
    with TestClient(data_app):
        data_app.state.repository.create_user(
            {
                "username": "admin",
                "role": "admin",
                "password_hash": password_hash,
            }
        )
        data = DataClient(
            base_url="http://data/internal/v1",
            health_url="http://data/health/ready",
            internal_token=settings.internal_token,
            transport=httpx.ASGITransport(app=data_app),
        )
        gate = asyncio.Event()
        release = asyncio.Event()
        rotate = data.rotate_refresh_token

        async def pause_rotation(jti, payload):
            if rotation_order == "before_logout":
                result = await rotate(jti, payload)
            gate.set()
            await release.wait()
            if rotation_order == "after_logout":
                result = await rotate(jti, payload)
            return result

        data.rotate_refresh_token = pause_rotation
        app = create_app(settings=settings, data_client=data)
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                original = await login(client)
                client.cookies.clear()
                pending = asyncio.create_task(
                    client.post(
                        "/api/v1/auth/refresh",
                        json={"refresh_token": original["refresh_token"]},
                    )
                )
                await asyncio.wait_for(gate.wait(), 2)
                try:
                    if logout_credential == "access":
                        response = await client.post(
                            "/api/v1/auth/logout",
                            headers={
                                "Authorization": f"Bearer {original['access_token']}"
                            },
                        )
                    else:
                        response = await client.post(
                            "/api/v1/auth/logout",
                            json={"refresh_token": original["refresh_token"]},
                        )
                    assert response.status_code == 204, response.text
                finally:
                    release.set()
                refreshed = await pending
                assert refreshed.status_code == (
                    200 if rotation_order == "before_logout" else 401
                )
                accesses = [original["access_token"]]
                if refreshed.status_code == 200:
                    accesses.append(refreshed.json()["access_token"])
                    assert (
                        await client.post(
                            "/api/v1/auth/refresh",
                            json={"refresh_token": refreshed.json()["refresh_token"]},
                        )
                    ).status_code == 401
                for token in accesses:
                    response = await client.get(
                        "/api/v1/cameras", headers={"Authorization": f"Bearer {token}"}
                    )
                    assert response.status_code == 401
        finally:
            await data.close()
