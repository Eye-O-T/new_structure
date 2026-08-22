from __future__ import annotations

from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import jwt
import pytest
from fastapi.testclient import TestClient

from server.services.external.app.config import PublishCredential, Settings
from server.services.external.app.data_client import DataForbidden, DataNotFound
from server.services.external.app.dependencies import (
    get_data_client,
    get_settings_dependency,
)
from server.services.external.app.main import create_app
from server.services.external.app.security import (
    TokenExpiredError,
    decode_token,
    hash_password,
    issue_token,
)


class FakeDataClient:
    def __init__(self, password_hash: str) -> None:
        self.users = {
            "1": {
                "id": "1",
                "username": "admin",
                "password_hash": password_hash,
                "role": "admin",
                "is_active": True,
            },
            "2": {
                "id": "2",
                "username": "viewer",
                "password_hash": password_hash,
                "role": "viewer",
                "is_active": True,
            },
        }
        self.refresh_tokens: dict[str, dict] = {}
        self.rotated_from: list[str] = []
        self.revoked_access: list[str] = []
        self.revoked_refresh: list[str] = []
        self.camera_acl_calls: list[tuple[str, str]] = []
        self.permission_calls: list[str] = []
        self.created_cameras: list[dict] = []
        self.deleted_cameras: list[str] = []
        self.publish_credentials: dict[str, dict] = {}

    async def health(self):
        return {"status": "ready"}

    async def get_user_by_username(self, username: str):
        for user in self.users.values():
            if user["username"] == username:
                return dict(user)
        raise DataNotFound()

    async def get_user(self, user_id: str):
        try:
            return dict(self.users[str(user_id)])
        except KeyError as exc:
            raise DataNotFound() from exc

    async def create_refresh_token(self, payload: dict):
        self.refresh_tokens[payload["jti"]] = dict(payload)
        return payload

    async def rotate_refresh_token(self, old_jti: str, payload: dict):
        if old_jti not in self.refresh_tokens:
            raise DataNotFound()
        self.rotated_from.append(old_jti)
        self.refresh_tokens.pop(old_jti)
        self.refresh_tokens[payload["jti"]] = dict(payload)
        return payload

    async def get_refresh_token(self, jti: str):
        try:
            return dict(self.refresh_tokens[jti])
        except KeyError as exc:
            raise DataNotFound() from exc

    async def revoke_refresh_token(self, jti: str):
        if jti not in self.refresh_tokens:
            raise DataNotFound()
        self.refresh_tokens.pop(jti)
        self.revoked_refresh.append(jti)

    async def is_access_token_revoked(self, jti: str):
        return jti in self.revoked_access

    async def revoke_access_token(self, jti: str, payload: dict):
        del payload
        self.revoked_access.append(jti)

    async def list_cameras(self, *, user_id: str, limit: int, offset: int):
        return {
            "items": [{"camera_id": "cam-001"}],
            "user_id": user_id,
            "limit": limit,
            "offset": offset,
        }

    async def get_camera(self, camera_id: str, *, user_id: str):
        self.camera_acl_calls.append((camera_id, user_id))
        if user_id == "2" and camera_id != "cam-001":
            raise DataForbidden()
        return {"camera_id": camera_id, "stream_path": camera_id}

    async def create_camera(self, payload: dict):
        self.created_cameras.append(dict(payload))
        return payload

    async def update_camera(self, camera_id: str, payload: dict):
        return {"camera_id": camera_id, **payload}

    async def delete_camera(self, camera_id: str):
        self.deleted_cameras.append(camera_id)
        self.publish_credentials.pop(camera_id, None)

    async def put_camera_publish_credential(self, camera_id: str, payload: dict):
        credential = {"camera_id": camera_id, **payload}
        self.publish_credentials[camera_id] = credential
        return credential

    async def get_camera_publish_credential(self, camera_id: str):
        return self.publish_credentials.get(camera_id)

    async def list_recordings(self, **params):
        return {"items": [], "query": params}

    async def get_recording(self, segment_id: str, *, user_id: str):
        return {
            "id": segment_id,
            "camera_id": "cam-001",
            "user_id": user_id,
            "start_time": "2026-08-22T08:00:00Z",
            "end_time": "2026-08-22T08:01:00Z",
        }

    async def list_events(self, **params):
        return {"items": [], "query": params}

    async def get_event(self, event_id: str, *, user_id: str):
        return {"id": event_id, "camera_id": "cam-001", "user_id": user_id}

    async def list_users(self, *, limit: int, offset: int):
        del limit, offset
        return {"items": list(self.users.values())}

    async def create_user(self, payload: dict):
        user_id = str(len(self.users) + 1)
        user = {"id": user_id, **payload}
        self.users[user_id] = user
        return dict(user)

    async def update_user(self, user_id: str, payload: dict):
        self.users[user_id].update(payload)
        return dict(self.users[user_id])

    async def get_camera_permissions(self, user_id: str):
        self.permission_calls.append(user_id)
        return {"user_id": user_id, "camera_ids": ["cam-001"]}

    async def set_camera_permissions(self, user_id: str, camera_ids: list[str]):
        return {"user_id": user_id, "camera_ids": camera_ids}


@pytest.fixture(scope="module")
def password_hash() -> str:
    return hash_password("correct horse battery staple")


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        data_base_url="http://data.test/internal/data/v1",
        data_health_url="http://data.test/internal/data/health/ready",
        internal_token="internal-token-for-tests",
        jwt_secret="s" * 32,
        cookie_secure=False,
        media_publish_credentials={
            "cam-001": PublishCredential(username="publisher", password="camera-secret")
        },
    )


@pytest.fixture()
def service(password_hash: str, settings: Settings):
    fake_data = FakeDataClient(password_hash)
    application = create_app()

    def override_settings():
        return settings

    async def override_data():
        return fake_data

    application.dependency_overrides[get_settings_dependency] = override_settings
    application.dependency_overrides[get_data_client] = override_data

    with TestClient(application) as client:
        yield client, fake_data


def _login(client: TestClient, username: str = "viewer") -> dict:
    response = client.post(
        "/api/v1/auth/login",
        json={"username": username, "password": "correct horse battery staple"},
    )
    assert response.status_code == 200
    return response.json()


def test_login_issues_hs256_claims_and_http_only_cookies(service, settings: Settings):
    client, fake_data = service
    response = client.post(
        "/api/v1/auth/login",
        json={"username": "viewer", "password": "correct horse battery staple"},
    )

    assert response.status_code == 200
    body = response.json()
    claims = jwt.decode(
        body["access_token"],
        settings.jwt_secret,
        algorithms=["HS256"],
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
    )
    assert claims["sub"] == "2"
    assert claims["role"] == "viewer"
    assert claims["type"] == "access"
    assert set(("sub", "role", "iat", "exp", "jti")) <= claims.keys()
    assert claims["exp"] > claims["iat"]
    assert len(fake_data.refresh_tokens) == 1
    set_cookie = "\n".join(response.headers.get_list("set-cookie"))
    assert "HttpOnly" in set_cookie
    assert settings.access_cookie_name in set_cookie
    assert settings.refresh_cookie_name in set_cookie


def test_expired_jwt_is_rejected(settings: Settings):
    expired = issue_token(
        settings,
        user_id="2",
        role="viewer",
        token_type="access",
        ttl_seconds=1,
        now=datetime.now(timezone.utc) - timedelta(minutes=2),
    )
    with pytest.raises(TokenExpiredError):
        decode_token(expired.encoded, settings, expected_type="access")


def test_viewer_cannot_create_camera(service):
    client, fake_data = service
    token = _login(client)["access_token"]

    response = client.post(
        "/api/v1/cameras",
        headers={"Authorization": f"Bearer {token}"},
        json={"camera_id": "cam-002", "name": "Side gate"},
    )

    assert response.status_code == 403
    assert fake_data.created_cameras == []


def test_admin_camera_create_returns_one_time_dynamic_publish_credential(service):
    client, fake_data = service
    token = _login(client, "admin")["access_token"]
    response = client.post(
        "/api/v1/cameras",
        headers={"Authorization": f"Bearer {token}"},
        json={"camera_id": "cam-002", "name": "Side gate"},
    )
    assert response.status_code == 201
    credential = response.json()["publish_credentials"]
    assert credential["username"] == "cam-002"
    assert len(credential["password"]) >= 32
    assert response.headers["cache-control"] == "no-store"
    assert fake_data.publish_credentials["cam-002"]["password_hash"].startswith(
        "$argon2"
    )

    authenticated = client.post(
        "/internal/media-auth",
        json={
            "action": "publish",
            "path": "cam-002",
            "user": credential["username"],
            "password": credential["password"],
        },
    )
    assert authenticated.status_code == 204


def test_camera_acl_is_enforced_by_data_user_id(service):
    client, fake_data = service
    token = _login(client)["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    allowed = client.get("/api/v1/cameras/cam-001", headers=headers)
    denied = client.get("/api/v1/cameras/cam-002", headers=headers)

    assert allowed.status_code == 200
    assert denied.status_code == 403
    assert ("cam-001", "2") in fake_data.camera_acl_calls
    assert ("cam-002", "2") not in fake_data.camera_acl_calls
    assert fake_data.permission_calls.count("2") >= 2


def test_recording_playback_uses_numeric_mediamtx_duration(service):
    client, _ = service
    token = _login(client)["access_token"]

    response = client.get(
        "/api/v1/recordings/segment-001/playback",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 200
    query = parse_qs(urlparse(response.json()["playback_url"]).query)
    assert query["path"] == ["cam-001"]
    assert query["duration"] == ["60.000"]


def test_internal_auth_verify_returns_identity_headers_and_checks_hls_acl(service):
    client, fake_data = service
    token = _login(client)["access_token"]

    response = client.get(
        "/internal/auth/verify",
        headers={
            "Authorization": f"Bearer {token}",
            "X-Original-URI": "/hls/cam-001/index.m3u8",
        },
    )

    assert response.status_code == 200
    assert response.headers["x-user-id"] == "2"
    assert response.headers["x-user-role"] == "viewer"
    assert fake_data.camera_acl_calls[-1] == ("cam-001", "2")


def test_refresh_rotates_and_logout_revokes_tokens(service):
    client, fake_data = service
    login = _login(client)
    old_refresh_claims = jwt.decode(
        login["refresh_token"], options={"verify_signature": False}
    )

    refreshed = client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": login["refresh_token"]},
    )

    assert refreshed.status_code == 200
    assert fake_data.rotated_from == [old_refresh_claims["jti"]]
    refreshed_body = refreshed.json()

    logout = client.post(
        "/api/v1/auth/logout",
        headers={"Authorization": f"Bearer {refreshed_body['access_token']}"},
        json={"refresh_token": refreshed_body["refresh_token"]},
    )
    assert logout.status_code == 204
    assert len(fake_data.revoked_access) == 1
    assert len(fake_data.revoked_refresh) == 1


def test_media_publish_auth_requires_matching_path_credentials(service):
    client, _ = service
    valid = client.post(
        "/internal/media-auth",
        json={
            "action": "publish",
            "path": "cam-001",
            "user": "publisher",
            "password": "camera-secret",
        },
    )
    wrong_path = client.post(
        "/internal/media-auth",
        json={
            "action": "publish",
            "path": "cam-002",
            "user": "publisher",
            "password": "camera-secret",
        },
    )
    excluded_action = client.post(
        "/internal/media-auth",
        json={"action": "read", "path": "not-a-camera"},
    )

    assert valid.status_code == 204
    assert wrong_path.status_code == 401
    assert wrong_path.json() == {"detail": "Media authentication failed"}
    assert excluded_action.status_code == 204
