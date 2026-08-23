from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import jwt
import httpx
import pytest
from fastapi.testclient import TestClient

from server.services.external.app.config import PublishCredential, Settings
from server.services.external.app.data_client import (
    DataClient,
    DataConflict,
    DataForbidden,
    DataNotFound,
    DataServiceUnavailable,
)
from server.services.external.app.edge_client import EdgeControlError, EdgeHttpClient
from server.services.external.app.dependencies import (
    get_data_client,
    get_settings_dependency,
)
from server.services.external.app.main import create_app
from server.services.external.app.media_client import MediaControlError, MediaMtxClient
from server.services.external.app.security import (
    TokenExpiredError,
    decode_token,
    hash_password,
    issue_token,
)
from server.services.external.app.status_collector import StatusCollector


MEDIA_READ_USERNAME = "inference-reader"
MEDIA_READ_PASSWORD = "r" * 32


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
        self.disconnected_publishers: list[str] = []
        self.publish_credentials: dict[str, dict] = {}
        self.publish_credential_camera_states: list[bool] = []
        self.camera_enabled: dict[str, bool] = {"cam-001": True}
        self.camera_deletable: dict[str, bool] = {"cam-001": True}
        self.video_profiles = {
            "cam-001": {
                "camera_id": "cam-001",
                "current_profile": "hd",
                "desired_profile": "hd",
                "supported_profiles": ["hd"],
                "edge_online": True,
                "last_error_code": None,
            }
        }
        self.profile_updates: list[dict] = []
        self.content_requests: list[tuple[str, str | None, str | None]] = []
        self.runtime_status = {
            "camera_id": "cam-001",
            "online": True,
            "cpu_percent": 12.0,
            "memory_percent": 34.0,
            "storage_percent": 56.0,
            "battery_percent": 78.0,
            "power_source": "external",
            "camera_input": "online",
            "central_connection_status": "online",
            "current_video_profile": "hd",
            "last_seen_at": "2026-08-23T07:20:00Z",
            "last_error_code": None,
        }
        self.edge_events: list[dict] = []

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
        return {
            "camera_id": camera_id,
            "stream_path": camera_id,
            "enabled": self.camera_enabled.get(camera_id, True),
        }

    async def create_camera(self, payload: dict):
        self.created_cameras.append(dict(payload))
        self.camera_enabled[payload["camera_id"]] = bool(payload.get("enabled", True))
        return payload

    async def update_camera(self, camera_id: str, payload: dict):
        if "enabled" in payload:
            self.camera_enabled[camera_id] = bool(payload["enabled"])
        return {"camera_id": camera_id, **payload}

    async def delete_camera(self, camera_id: str):
        self.deleted_cameras.append(camera_id)
        self.camera_enabled.pop(camera_id, None)
        self.publish_credentials.pop(camera_id, None)

    async def get_camera_deletion_status(self, camera_id: str):
        return {
            "camera_id": camera_id,
            "deletable": self.camera_deletable.get(camera_id, True),
            "reason_code": (
                None
                if self.camera_deletable.get(camera_id, True)
                else "CAMERA_HAS_HISTORY"
            ),
        }

    async def put_camera_publish_credential(self, camera_id: str, payload: dict):
        self.publish_credential_camera_states.append(
            self.camera_enabled.get(camera_id, True)
        )
        credential = {"camera_id": camera_id, **payload}
        self.publish_credentials[camera_id] = credential
        return credential

    async def get_camera_publish_credential(self, camera_id: str):
        return self.publish_credentials.get(camera_id)

    async def get_camera_control_target(self, camera_id: str):
        return {
            "camera_id": camera_id,
            "edge_device_id": "edge-001",
            "management_url": "http://edge.test:8003",
            "auth_token": "e" * 32,
        }

    async def get_camera_video_profile(self, camera_id: str):
        return dict(self.video_profiles[camera_id])

    async def update_camera_video_profile(self, camera_id: str, payload: dict):
        self.profile_updates.append(dict(payload))
        self.video_profiles.setdefault(camera_id, {"camera_id": camera_id}).update(
            payload
        )
        return dict(self.video_profiles[camera_id])

    async def get_camera_runtime_status(self, camera_id: str):
        return {**self.runtime_status, "camera_id": camera_id}

    async def put_camera_runtime_status(self, camera_id: str, payload: dict):
        self.runtime_status.update(payload)
        self.runtime_status["camera_id"] = camera_id
        return dict(self.runtime_status)

    async def create_event(self, payload: dict):
        self.edge_events.append(dict(payload))
        return {"id": len(self.edge_events), **payload}

    async def list_recovery_jobs(self, **params):
        return {"items": [], "query": params}

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

    async def open_recording_content(
        self,
        segment_id: str,
        *,
        range_header: str | None = None,
        if_range_header: str | None = None,
    ) -> httpx.Response:
        self.content_requests.append((segment_id, range_header, if_range_header))
        content = b"recovered-mpegts"
        headers = {
            "content-type": "video/mp2t",
            "accept-ranges": "bytes",
        }
        status_code = 200
        if range_header == "bytes=2-6":
            content = content[2:7]
            status_code = 206
            headers["content-range"] = "bytes 2-6/17"
        headers["content-length"] = str(len(content))
        return httpx.Response(
            status_code,
            content=content,
            headers=headers,
            request=httpx.Request("GET", "http://data.test/content"),
        )

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
        return {"items": [{"camera_id": "cam-001"}]}

    async def set_camera_permissions(self, user_id: str, camera_ids: list[str]):
        del user_id
        return {"items": [{"camera_id": camera_id} for camera_id in camera_ids]}


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
        media_read_username=MEDIA_READ_USERNAME,
        media_read_password=MEDIA_READ_PASSWORD,
        cookie_secure=False,
        media_publish_credentials={
            "cam-001": PublishCredential(username="publisher", password="camera-secret")
        },
    )


@pytest.fixture()
def service(password_hash: str, settings: Settings, monkeypatch):
    fake_data = FakeDataClient(password_hash)
    application = create_app()

    async def disconnect_publisher(_settings, camera_id):
        fake_data.disconnected_publishers.append(camera_id)
        return True

    monkeypatch.setattr(
        "server.services.external.app.main._disconnect_camera_publisher",
        disconnect_publisher,
    )

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
    assert fake_data.created_cameras[-1]["enabled"] is False
    assert fake_data.created_cameras[-1]["status"] == "disabled"
    assert fake_data.publish_credential_camera_states[-1] is False
    assert fake_data.camera_enabled["cam-002"] is True

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


def test_camera_create_rollback_kicks_publisher_before_deleting_disabled_row(
    service, monkeypatch
):
    client, fake_data = service

    async def fail_credential(_camera_id: str, _payload: dict):
        assert fake_data.camera_enabled["cam-002"] is False
        raise DataServiceUnavailable("credential store unavailable")

    monkeypatch.setattr(fake_data, "put_camera_publish_credential", fail_credential)
    token = _login(client, "admin")["access_token"]
    response = client.post(
        "/api/v1/cameras",
        headers={"Authorization": f"Bearer {token}"},
        json={"camera_id": "cam-002", "name": "Side gate"},
    )

    assert response.status_code == 503
    assert fake_data.disconnected_publishers == ["cam-002"]
    assert fake_data.deleted_cameras == ["cam-002"]
    assert "cam-002" not in fake_data.camera_enabled


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


def test_internal_auth_verify_checks_playback_path_acl(service):
    client, _fake_data = service
    token = _login(client)["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    allowed = client.get(
        "/internal/auth/verify",
        headers={
            **headers,
            "X-Original-URI": "/playback/get?path=cam-001&format=fmp4",
        },
    )
    denied = client.get(
        "/internal/auth/verify",
        headers={
            **headers,
            "X-Original-URI": "/playback/get?path=cam-002&format=fmp4",
        },
    )
    missing = client.get(
        "/internal/auth/verify",
        headers={**headers, "X-Original-URI": "/playback/get?format=fmp4"},
    )
    duplicate = client.get(
        "/internal/auth/verify",
        headers={
            **headers,
            "X-Original-URI": "/playback/get?path=cam-001&path=cam-002",
        },
    )

    assert allowed.status_code == 200
    assert denied.status_code == 403
    assert missing.status_code == 400
    assert duplicate.status_code == 400


def test_internal_auth_verify_rejects_camera_selector_acl_bypass(service):
    client, _fake_data = service
    token = _login(client)["access_token"]
    base_headers = {
        "Authorization": f"Bearer {token}",
        "X-Camera-ID": "cam-001",
    }

    hls_conflict = client.get(
        "/internal/auth/verify",
        headers={
            **base_headers,
            "X-Original-URI": "/hls/cam-002/index.m3u8",
        },
    )
    playback_conflict = client.get(
        "/internal/auth/verify",
        headers={
            **base_headers,
            "X-Original-URI": "/playback/get?path=cam-002&format=fmp4",
        },
    )
    assert hls_conflict.status_code == 400
    assert playback_conflict.status_code == 400


def test_internal_auth_verify_rejects_encoded_hls_path_confusion(service):
    client, fake_data = service
    token = _login(client)["access_token"]
    before = list(fake_data.camera_acl_calls)

    ambiguous_paths = (
        "/hls/cam-001/%252e%252e/cam-002/index.m3u8",
        "/%68ls/cam-002/index.m3u8",
        "//hls/cam-002/index.m3u8",
        "/%70layback/get?path=cam-002&format=fmp4",
    )
    for path in ambiguous_paths:
        response = client.get(
            "/internal/auth/verify",
            headers={
                "Authorization": f"Bearer {token}",
                "X-Original-URI": path,
            },
        )
        assert response.status_code == 400, path
    assert fake_data.camera_acl_calls == before


def test_public_openapi_excludes_internal_routes(service):
    client, _fake_data = service

    response = client.get("/api/v1/openapi.json")

    assert response.status_code == 200
    paths = response.json()["paths"]
    assert "/api/v1/cameras/{camera_id}/status" in paths
    assert "/api/v1/cameras/{camera_id}/video-profile" in paths
    assert "/api/v1/cameras/{camera_id}/live" in paths
    assert "/health/live" not in paths
    assert "/health/ready" not in paths
    assert not any(path.startswith("/internal/") for path in paths)
    schemas = response.json()["components"]["schemas"]
    assert set(schemas["CameraLiveResponse"]["properties"]) >= {
        "camera_id",
        "protocol",
        "url",
        "hls_url",
        "auth",
    }
    assert set(schemas["VideoProfileResponse"]["properties"]) >= {
        "camera_id",
        "current_profile",
        "desired_profile",
        "supported_profiles",
        "edge_online",
        "last_error_code",
    }
    assert set(schemas["CameraStatusResponse"]["properties"]) >= {
        "camera_id",
        "online",
        "camera_input",
        "central_connection_status",
        "current_video_profile",
    }
    camera_page = schemas["CameraPageResponse"]["properties"]
    assert camera_page["items"]["items"]["$ref"].endswith("/CameraResponse")
    assert camera_page["limit"]["type"] == "integer"
    assert paths["/api/v1/cameras/{camera_id}/status"]["get"]["responses"]["200"][
        "content"
    ]["application/json"]["schema"]["$ref"].endswith("/CameraStatusResponse")
    expected_contracts = {
        "TokenResponse": {"access_token", "refresh_token", "expires_in", "user"},
        "RecordingResponse": {
            "id",
            "camera_id",
            "start_time",
            "end_time",
            "format",
            "source",
        },
        "EventResponse": {"id", "camera_id", "event_type", "occurred_at"},
        "UserResponse": {"id", "username", "role", "is_active"},
        "RecoveryJobResponse": {
            "id",
            "camera_id",
            "outage_started_at",
            "outage_ended_at",
            "status",
            "attempt_count",
        },
    }
    for schema_name, fields in expected_contracts.items():
        assert set(schemas[schema_name]["properties"]) >= fields
    page_items = {
        "RecordingPageResponse": "RecordingResponse",
        "EventPageResponse": "EventResponse",
        "UserPageResponse": "UserResponse",
        "RecoveryJobPageResponse": "RecoveryJobResponse",
    }
    for page_name, item_name in page_items.items():
        assert schemas[page_name]["properties"]["items"]["items"]["$ref"].endswith(
            f"/{item_name}"
        )
    assert set(schemas["SystemStatusResponse"]["properties"]) == {
        "external",
        "data",
    }
    assert schemas["CameraPermissionListResponse"]["properties"]["items"]["items"][
        "$ref"
    ].endswith("/CameraResponse")
    content_schema = paths["/api/v1/recordings/{segment_id}/content"]["get"][
        "responses"
    ]["206"]["content"]["video/mp2t"]["schema"]
    assert content_schema == {"type": "string", "format": "binary"}


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


def test_media_auth_separates_publish_rtsp_read_and_internal_hls(service):
    client, fake_data = service
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
    valid_rtsp_read = client.post(
        "/internal/media-auth",
        json={
            "action": "read",
            "protocol": "rtsp",
            "path": "cam-001",
            "user": MEDIA_READ_USERNAME,
            "password": MEDIA_READ_PASSWORD,
        },
    )
    wrong_rtsp_read = client.post(
        "/internal/media-auth",
        json={
            "action": "read",
            "protocol": "rtsp",
            "path": "cam-001",
            "user": MEDIA_READ_USERNAME,
            "password": "wrong-reader-password",
        },
    )
    internal_hls = client.post(
        "/internal/media-auth",
        json={"action": "read", "protocol": "hls", "path": "not-a-camera"},
    )
    fake_data.camera_enabled["cam-001"] = False
    disabled_rtsp_read = client.post(
        "/internal/media-auth",
        json={
            "action": "read",
            "protocol": "rtsp",
            "path": "cam-001",
            "user": MEDIA_READ_USERNAME,
            "password": MEDIA_READ_PASSWORD,
        },
    )

    assert valid.status_code == 204
    assert wrong_path.status_code == 401
    assert wrong_path.json() == {"detail": "Media authentication failed"}
    assert valid_rtsp_read.status_code == 204
    assert wrong_rtsp_read.status_code == 401
    assert internal_hls.status_code == 204
    assert disabled_rtsp_read.status_code == 401
    mediamtx = Path("server/mediamtx/mediamtx.yml").read_text(encoding="utf-8")
    assert "  - action: read" not in mediamtx


def test_reregistered_camera_db_publish_credential_overrides_static(service):
    client, _fake_data = service
    admin = _login(client, "admin")["access_token"]
    headers = {"Authorization": f"Bearer {admin}"}

    assert client.delete("/api/v1/cameras/cam-001", headers=headers).status_code == 204
    registered = client.post(
        "/api/v1/cameras",
        headers=headers,
        json={"camera_id": "cam-001", "name": "Entrance"},
    )
    assert registered.status_code == 201, registered.text
    credential = registered.json()["publish_credentials"]

    current = client.post(
        "/internal/media-auth",
        json={
            "action": "publish",
            "path": "cam-001",
            "user": credential["username"],
            "password": credential["password"],
        },
    )
    stale_static = client.post(
        "/internal/media-auth",
        json={
            "action": "publish",
            "path": "cam-001",
            "user": "publisher",
            "password": "camera-secret",
        },
    )
    assert current.status_code == 204
    assert stale_static.status_code == 401
    assert _fake_data.disconnected_publishers == ["cam-001"]


def test_live_contract_covers_hls_cookie_and_segment_acl(service):
    client, fake_data = service
    token = _login(client)["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    live = client.get("/api/v1/cameras/cam-001/live", headers=headers)
    assert live.status_code == 200
    assert live.json() == {
        "camera_id": "cam-001",
        "protocol": "hls",
        "url": "/hls/cam-001/index.m3u8",
        "hls_url": "/hls/cam-001/index.m3u8",
        "auth": {"method": "cookie", "cookie_name": "ai_cctv_access"},
    }
    segment = client.get(
        "/internal/auth/verify",
        headers={**headers, "X-Original-URI": "/hls/cam-001/segment0001.ts"},
    )
    denied = client.get(
        "/internal/auth/verify",
        headers={**headers, "X-Original-URI": "/hls/cam-002/segment0001.ts"},
    )
    assert segment.status_code == 200
    assert denied.status_code == 403
    assert fake_data.camera_acl_calls[-1] != ("cam-002", "2")


def test_disabling_camera_blocks_live_and_clears_runtime_state(service):
    client, fake_data = service
    admin = _login(client, "admin")["access_token"]
    viewer = _login(client)["access_token"]

    disabled = client.patch(
        "/api/v1/cameras/cam-001",
        headers={"Authorization": f"Bearer {admin}"},
        json={"enabled": False},
    )
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["enabled"] is False

    live = client.get(
        "/api/v1/cameras/cam-001/live",
        headers={"Authorization": f"Bearer {viewer}"},
    )
    hls_auth = client.get(
        "/internal/auth/verify",
        headers={
            "Authorization": f"Bearer {viewer}",
            "X-Original-URI": "/hls/cam-001/index.m3u8",
        },
    )
    status_response = client.get(
        "/api/v1/cameras/cam-001/status",
        headers={"Authorization": f"Bearer {viewer}"},
    )
    assert live.status_code == 409
    assert hls_auth.status_code == 403
    assert status_response.status_code == 200
    assert status_response.json()["online"] is False
    assert status_response.json()["camera_input"] == "unknown"
    assert status_response.json()["central_connection_status"] == "unknown"
    assert status_response.json()["last_error_code"] == "CAMERA_DISABLED"
    assert fake_data.camera_enabled["cam-001"] is False
    assert fake_data.disconnected_publishers == ["cam-001"]


def test_camera_delete_history_conflict_does_not_disable_or_kick(service):
    client, fake_data = service
    fake_data.camera_deletable["cam-001"] = False
    admin = _login(client, "admin")["access_token"]

    response = client.delete(
        "/api/v1/cameras/cam-001",
        headers={"Authorization": f"Bearer {admin}"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CAMERA_HAS_HISTORY"
    assert fake_data.camera_enabled["cam-001"] is True
    assert fake_data.disconnected_publishers == []
    assert fake_data.deleted_cameras == []


def test_admin_rotates_publish_credential_and_old_password_stops_working(service):
    client, fake_data = service
    admin = _login(client, "admin")["access_token"]
    headers = {"Authorization": f"Bearer {admin}"}

    rotated = client.post(
        "/api/v1/cameras/cam-001/publish-credentials/rotate",
        headers=headers,
    )

    assert rotated.status_code == 200, rotated.text
    credential = rotated.json()["publish_credentials"]
    assert credential["username"] == "cam-001"
    assert len(credential["password"]) >= 32
    assert rotated.headers["cache-control"] == "no-store"
    assert fake_data.disconnected_publishers == ["cam-001"]
    assert fake_data.camera_enabled["cam-001"] is True

    fresh = client.post(
        "/internal/media-auth",
        json={
            "action": "publish",
            "path": "cam-001",
            "user": "cam-001",
            "password": credential["password"],
        },
    )
    stale = client.post(
        "/internal/media-auth",
        json={
            "action": "publish",
            "path": "cam-001",
            "user": "publisher",
            "password": "camera-secret",
        },
    )
    assert fresh.status_code == 204
    assert stale.status_code == 401


def test_camera_lifecycle_lock_pool_is_stable_and_bounded(password_hash: str):
    application = create_app(data_client=FakeDataClient(password_hash))
    lock_factory = application.state.camera_lifecycle_lock_factory

    assert lock_factory("cam-001") is lock_factory("cam-001")
    locks = {lock_factory(f"attacker-path-{index}") for index in range(1_000)}
    assert len(locks) <= 64


@pytest.mark.asyncio
async def test_camera_lifecycle_lock_preserves_newer_disable_during_rotation(
    password_hash: str, settings: Settings, monkeypatch
) -> None:
    fake_data = FakeDataClient(password_hash)
    application = create_app(data_client=fake_data)
    application.dependency_overrides[get_settings_dependency] = lambda: settings
    disconnect_started = asyncio.Event()
    release_disconnect = asyncio.Event()
    disconnect_calls: list[str] = []

    async def gated_disconnect(_settings: Settings, camera_id: str) -> bool:
        disconnect_calls.append(camera_id)
        if len(disconnect_calls) == 1:
            disconnect_started.set()
            await release_disconnect.wait()
        return True

    monkeypatch.setattr(
        "server.services.external.app.main._disconnect_camera_publisher",
        gated_disconnect,
    )
    admin_token = issue_token(
        settings,
        user_id="1",
        role="admin",
        token_type="access",
        ttl_seconds=60,
    ).encoded
    headers = {"Authorization": f"Bearer {admin_token}"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        rotate_task = asyncio.create_task(
            client.post(
                "/api/v1/cameras/cam-001/publish-credentials/rotate",
                headers=headers,
            )
        )
        await asyncio.wait_for(disconnect_started.wait(), timeout=1)
        disable_task = asyncio.create_task(
            client.patch(
                "/api/v1/cameras/cam-001",
                headers=headers,
                json={"enabled": False},
            )
        )
        await asyncio.sleep(0.01)
        assert not disable_task.done()

        release_disconnect.set()
        rotated, disabled = await asyncio.gather(rotate_task, disable_task)

    assert rotated.status_code == 200, rotated.text
    assert disabled.status_code == 200, disabled.text
    assert disabled.json()["enabled"] is False
    assert fake_data.camera_enabled["cam-001"] is False
    assert disconnect_calls == ["cam-001", "cam-001"]


@pytest.mark.asyncio
async def test_camera_lifecycle_lock_serializes_concurrent_rotations(
    password_hash: str, settings: Settings, monkeypatch
) -> None:
    fake_data = FakeDataClient(password_hash)
    application = create_app(data_client=fake_data)
    application.dependency_overrides[get_settings_dependency] = lambda: settings
    first_read_started = asyncio.Event()
    release_first_read = asyncio.Event()
    original_get_camera = fake_data.get_camera
    read_calls = 0
    active_reads = 0
    maximum_active_reads = 0

    async def gated_get_camera(camera_id: str, *, user_id: str):
        nonlocal read_calls, active_reads, maximum_active_reads
        read_calls += 1
        active_reads += 1
        maximum_active_reads = max(maximum_active_reads, active_reads)
        try:
            if read_calls == 1:
                first_read_started.set()
                await release_first_read.wait()
            return await original_get_camera(camera_id, user_id=user_id)
        finally:
            active_reads -= 1

    async def disconnect_publisher(_settings: Settings, camera_id: str) -> bool:
        fake_data.disconnected_publishers.append(camera_id)
        return True

    monkeypatch.setattr(fake_data, "get_camera", gated_get_camera)
    monkeypatch.setattr(
        "server.services.external.app.main._disconnect_camera_publisher",
        disconnect_publisher,
    )
    admin_token = issue_token(
        settings,
        user_id="1",
        role="admin",
        token_type="access",
        ttl_seconds=60,
    ).encoded
    headers = {"Authorization": f"Bearer {admin_token}"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        first_task = asyncio.create_task(
            client.post(
                "/api/v1/cameras/cam-001/publish-credentials/rotate",
                headers=headers,
            )
        )
        await asyncio.wait_for(first_read_started.wait(), timeout=1)
        second_task = asyncio.create_task(
            client.post(
                "/api/v1/cameras/cam-001/publish-credentials/rotate",
                headers=headers,
            )
        )
        await asyncio.sleep(0.01)
        assert read_calls == 1
        assert not second_task.done()

        release_first_read.set()
        first, second = await asyncio.gather(first_task, second_task)
        rotation_read_calls = read_calls
        first_password = first.json()["publish_credentials"]["password"]
        second_password = second.json()["publish_credentials"]["password"]
        first_auth = await client.post(
            "/internal/media-auth",
            json={
                "action": "publish",
                "path": "cam-001",
                "user": "cam-001",
                "password": first_password,
            },
        )
        second_auth = await client.post(
            "/internal/media-auth",
            json={
                "action": "publish",
                "path": "cam-001",
                "user": "cam-001",
                "password": second_password,
            },
        )

    assert first.status_code == 200, first.text
    assert second.status_code == 200, second.text
    assert first_password != second_password
    assert rotation_read_calls == 2
    assert maximum_active_reads == 1
    assert fake_data.disconnected_publishers == ["cam-001", "cam-001"]
    assert first_auth.status_code == 401
    assert second_auth.status_code == 204


@pytest.mark.asyncio
async def test_media_auth_finishes_before_concurrent_disable_closes_admission(
    password_hash: str, settings: Settings, monkeypatch
) -> None:
    fake_data = FakeDataClient(password_hash)
    application = create_app(data_client=fake_data)
    application.dependency_overrides[get_settings_dependency] = lambda: settings
    auth_state_read = asyncio.Event()
    release_auth = asyncio.Event()
    original_get_camera = fake_data.get_camera

    async def gated_get_camera(camera_id: str, *, user_id: str):
        camera = await original_get_camera(camera_id, user_id=user_id)
        if user_id == "0" and not auth_state_read.is_set():
            auth_state_read.set()
            await release_auth.wait()
        return camera

    async def disconnect_publisher(_settings: Settings, camera_id: str) -> bool:
        fake_data.disconnected_publishers.append(camera_id)
        return True

    auth_body_send_started = asyncio.Event()
    release_auth_response = asyncio.Event()

    async def delayed_auth_response(scope, receive, send):
        async def gated_send(message):
            if (
                scope.get("path") == "/internal/media-auth"
                and message["type"] == "http.response.body"
            ):
                auth_body_send_started.set()
                await release_auth_response.wait()
            await send(message)

        await application(scope, receive, gated_send)

    monkeypatch.setattr(fake_data, "get_camera", gated_get_camera)
    monkeypatch.setattr(
        "server.services.external.app.main._disconnect_camera_publisher",
        disconnect_publisher,
    )
    admin_token = issue_token(
        settings,
        user_id="1",
        role="admin",
        token_type="access",
        ttl_seconds=60,
    ).encoded
    headers = {"Authorization": f"Bearer {admin_token}"}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=delayed_auth_response),
        base_url="http://test",
    ) as client:
        auth_task = asyncio.create_task(
            client.post(
                "/internal/media-auth",
                json={
                    "action": "publish",
                    "protocol": "rtsp",
                    "path": "cam-001",
                    "user": "publisher",
                    "password": "camera-secret",
                },
            )
        )
        await asyncio.wait_for(auth_state_read.wait(), timeout=1)
        disable_task = asyncio.create_task(
            client.patch(
                "/api/v1/cameras/cam-001",
                headers=headers,
                json={"enabled": False},
            )
        )
        await asyncio.sleep(0.01)
        assert not disable_task.done()

        release_auth.set()
        await asyncio.wait_for(auth_body_send_started.wait(), timeout=1)
        await asyncio.sleep(0.01)
        assert not disable_task.done()
        release_auth_response.set()
        auth_response, disabled = await asyncio.gather(auth_task, disable_task)

    assert auth_response.status_code == 204
    assert disabled.status_code == 200, disabled.text
    assert fake_data.camera_enabled["cam-001"] is False
    assert fake_data.disconnected_publishers == ["cam-001"]


@pytest.mark.asyncio
async def test_mediamtx_client_kicks_late_attaching_camera_publisher():
    requests: list[tuple[str, str]] = []
    get_count = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal get_count
        requests.append((request.method, request.url.path))
        if request.method == "GET":
            get_count += 1
            if get_count in {1, 3, 4}:
                return httpx.Response(404)
            return httpx.Response(
                200,
                json={
                    "name": "cam-001",
                    "ready": True,
                    "source": {"type": "rtspSession", "id": "session-123"},
                },
            )
        return httpx.Response(200)

    client = MediaMtxClient(
        "http://media.test/internal/media",
        verification_interval_seconds=0,
        verification_quiet_checks=2,
        verification_max_checks=8,
        transport=httpx.MockTransport(handler),
    )
    try:
        assert await client.disconnect_publisher("cam-001") is True
    finally:
        await client.close()

    assert requests == [
        ("GET", "/internal/media/v3/paths/get/cam-001"),
        ("GET", "/internal/media/v3/paths/get/cam-001"),
        ("POST", "/internal/media/v3/rtspsessions/kick/session-123"),
        ("GET", "/internal/media/v3/paths/get/cam-001"),
        ("GET", "/internal/media/v3/paths/get/cam-001"),
    ]


@pytest.mark.asyncio
async def test_mediamtx_client_default_requires_extended_quiet_window():
    checks = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal checks
        checks += 1
        return httpx.Response(404)

    client = MediaMtxClient(
        "http://media.test/internal/media",
        verification_interval_seconds=0,
        transport=httpx.MockTransport(handler),
    )
    try:
        assert await client.disconnect_publisher("cam-001") is False
    finally:
        await client.close()

    assert checks == 10


@pytest.mark.asyncio
async def test_mediamtx_client_fails_closed_when_publisher_never_quiesces():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "name": "cam-001",
                    "ready": True,
                    "source": {"type": "rtspSession", "id": "persistent-session"},
                },
            )
        return httpx.Response(200)

    client = MediaMtxClient(
        "http://media.test/internal/media",
        verification_interval_seconds=0,
        verification_quiet_checks=2,
        verification_max_checks=3,
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(MediaControlError) as captured:
            await client.disconnect_publisher("cam-001")
    finally:
        await client.close()

    assert captured.value.code == "MEDIA_PUBLISHER_STILL_ACTIVE"


def test_disable_is_fail_closed_when_mediamtx_control_is_unavailable(
    service, monkeypatch
):
    client, fake_data = service

    async def fail_disconnect(_settings, _camera_id):
        raise MediaControlError("MediaMTX control API is unavailable.")

    monkeypatch.setattr(
        "server.services.external.app.main._disconnect_camera_publisher",
        fail_disconnect,
    )
    admin = _login(client, "admin")["access_token"]
    response = client.patch(
        "/api/v1/cameras/cam-001",
        headers={"Authorization": f"Bearer {admin}"},
        json={"enabled": False},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "MEDIA_CONTROL_UNAVAILABLE"
    assert fake_data.camera_enabled["cam-001"] is False


@pytest.mark.asyncio
async def test_data_client_preserves_allowlisted_camera_conflict():
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            json={
                "error": {
                    "code": "CAMERA_HAS_HISTORY",
                    "message": "Camera history must be retained; disable instead.",
                    "details": {},
                }
            },
        )

    client = DataClient(
        base_url="http://data.test/internal/v1",
        health_url="http://data.test/health",
        internal_token="internal-test-token",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(DataConflict) as captured:
            await client.delete_camera("cam-001")
    finally:
        await client.close()

    assert captured.value.code == "CAMERA_HAS_HISTORY"
    assert "disable" in captured.value.message


def test_hls_manifest_and_segment_expiry_then_refresh_cookie_recovery(
    service, settings: Settings
) -> None:
    client, _fake_data = service
    expired = issue_token(
        settings,
        user_id="2",
        role="viewer",
        token_type="access",
        ttl_seconds=1,
        now=datetime.now(timezone.utc) - timedelta(minutes=2),
    ).encoded
    for path in (
        "/hls/cam-001/index.m3u8",
        "/hls/cam-001/segment0001.ts",
    ):
        response = client.get(
            "/internal/auth/verify",
            headers={
                "Authorization": f"Bearer {expired}",
                "X-Original-URI": path,
            },
        )
        assert response.status_code == 401

    login = _login(client)
    refreshed = client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": login["refresh_token"]},
    )
    assert refreshed.status_code == 200
    # No Authorization header: auth_request uses the refreshed Secure/HttpOnly
    # access cookie on both manifest and segment subrequests.
    for path in (
        "/hls/cam-001/index.m3u8",
        "/hls/cam-001/segment0001.ts",
    ):
        response = client.get("/internal/auth/verify", headers={"X-Original-URI": path})
        assert response.status_code == 200


def test_nginx_hls_auth_subrequest_forwards_bearer_and_cookie() -> None:
    nginx = Path("server/nginx/nginx.conf").read_text(encoding="utf-8")
    hls = nginx.split("location ^~ /hls/ {", 1)[1].split("# Range is preserved", 1)[0]
    auth = nginx.split("location = /_auth {", 1)[1].split("}", 1)[0]
    assert "auth_request /_auth;" in hls
    assert 'if ($request_uri ~ "^[^?]*%") { return 400; }' in hls
    playback = nginx.split("location ^~ /playback/ {", 1)[1].split(
        "location = /_auth", 1
    )[0]
    assert 'if ($request_uri ~ "^[^?]*%") { return 400; }' in playback
    assert "proxy_set_header Authorization $http_authorization;" in auth
    assert "proxy_set_header Cookie $http_cookie;" in auth
    assert 'proxy_set_header X-Camera-ID "";' in auth
    assert "proxy_pass http://external_service/internal/auth/verify?;" in auth


def test_public_camera_response_never_exposes_edge_or_rtsp_metadata(service):
    client, fake_data = service
    token = _login(client, "admin")["access_token"]
    response = client.post(
        "/api/v1/cameras",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "camera_id": "cam-003",
            "name": "Back gate",
            "source_url": "rtsp://publisher:secret@10.0.0.3:8554/live",
            "edge_device_id": "edge-003",
            "edge_management_url": "http://10.0.0.3:8003",
            "edge_recovery_url": "http://10.0.0.3:8002",
            "edge_auth_token": "s" * 32,
        },
    )
    assert response.status_code == 201, response.text
    public = response.json()
    for field in (
        "source_url",
        "edge_device_id",
        "edge_management_url",
        "edge_recovery_url",
        "edge_auth_token",
    ):
        assert field not in public
    assert fake_data.created_cameras[-1]["edge_auth_token"] == "s" * 32


def test_video_profile_updates_current_only_after_edge_applied(
    service, monkeypatch: pytest.MonkeyPatch
):
    client, fake_data = service
    token = _login(client, "admin")["access_token"]

    class FakeEdge:
        supported = ["hd"]

        def __init__(self, **_kwargs):
            pass

        async def get_video_capabilities(self):
            return {
                "camera_id": "cam-001",
                "supported_profiles": list(self.supported),
                "current_profile": "hd",
                "encoder": "v4l2h264enc",
            }

        async def apply_video_profile(self, profile: str):
            return {
                "status": "applied",
                "previous_profile": "hd",
                "current_profile": profile,
            }

        async def close(self):
            return None

    monkeypatch.setattr("server.services.external.app.main.EdgeHttpClient", FakeEdge)
    rejected = client.patch(
        "/api/v1/cameras/cam-001/video-profile",
        headers={"Authorization": f"Bearer {token}"},
        json={"profile": "fhd"},
    )
    assert rejected.status_code == 409
    assert rejected.json()["error"]["code"] == "UNSUPPORTED_VIDEO_PROFILE"
    assert fake_data.video_profiles["cam-001"]["desired_profile"] == "fhd"
    assert fake_data.video_profiles["cam-001"]["current_profile"] == "hd"

    FakeEdge.supported = ["hd", "fhd"]
    applied = client.patch(
        "/api/v1/cameras/cam-001/video-profile",
        headers={"Authorization": f"Bearer {token}"},
        json={"profile": "fhd"},
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["current_profile"] == "fhd"
    current_updates = [
        item for item in fake_data.profile_updates if "current_profile" in item
    ]
    assert current_updates == [
        {
            "current_profile": "fhd",
            "supported_profiles": ["hd", "fhd"],
            "encoder": "v4l2h264enc",
            "last_error_code": None,
        }
    ]
    # The unsupported capability was rejected centrally before PUT and needs
    # an audit event. A successful PUT is journaled by Edge and must not be
    # duplicated by External before the collector imports it.
    assert [event["event_type"] for event in fake_data.edge_events] == [
        "video_profile_change_failed"
    ]


@pytest.mark.parametrize(
    ("capability_state", "expected_code"),
    [
        (
            {
                "capability_status": "unavailable",
                "camera_available": False,
                "encoder_available": True,
            },
            "CAMERA_UNAVAILABLE",
        ),
        (
            {
                "capability_status": "unavailable",
                "camera_available": True,
                "encoder_available": False,
            },
            "ENCODER_UNAVAILABLE",
        ),
        (
            {
                "capability_status": "unknown",
                "camera_available": None,
                "encoder_available": True,
            },
            "CAPABILITY_UNKNOWN",
        ),
    ],
)
def test_video_profile_maps_unavailable_capabilities_before_empty_profile_list(
    service,
    monkeypatch: pytest.MonkeyPatch,
    capability_state: dict,
    expected_code: str,
):
    client, fake_data = service
    token = _login(client, "admin")["access_token"]

    class UnavailableEdge:
        apply_calls = 0

        def __init__(self, **_kwargs):
            pass

        async def get_video_capabilities(self):
            return {
                "camera_id": "cam-001",
                "supported_profiles": [],
                "current_profile": "hd",
                "encoder": "v4l2h264enc",
                **capability_state,
            }

        async def apply_video_profile(self, _profile: str):
            type(self).apply_calls += 1
            return {"status": "applied", "current_profile": "fhd"}

        async def close(self):
            return None

    monkeypatch.setattr(
        "server.services.external.app.main.EdgeHttpClient", UnavailableEdge
    )
    response = client.patch(
        "/api/v1/cameras/cam-001/video-profile",
        headers={"Authorization": f"Bearer {token}"},
        json={"profile": "fhd"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == expected_code
    assert UnavailableEdge.apply_calls == 0
    assert fake_data.video_profiles["cam-001"]["current_profile"] == "hd"
    assert fake_data.video_profiles["cam-001"]["last_error_code"] == expected_code
    assert fake_data.edge_events[-1]["metadata"]["reason_code"] == expected_code


@pytest.mark.asyncio
async def test_video_profile_lock_keeps_concurrent_edge_and_data_state_consistent(
    password_hash: str, settings: Settings, monkeypatch
) -> None:
    fake_data = FakeDataClient(password_hash)
    application = create_app(data_client=fake_data)
    application.dependency_overrides[get_settings_dependency] = lambda: settings
    first_apply_started = asyncio.Event()
    release_first_apply = asyncio.Event()

    class SerializedEdge:
        current_profile = "hd"
        apply_calls: list[str] = []

        def __init__(self, **_kwargs):
            pass

        async def get_video_capabilities(self):
            return {
                "camera_id": "cam-001",
                "supported_profiles": ["hd", "fhd"],
                "current_profile": type(self).current_profile,
                "encoder": "v4l2h264enc",
            }

        async def apply_video_profile(self, profile: str):
            previous = type(self).current_profile
            type(self).apply_calls.append(profile)
            if profile == "fhd" and not first_apply_started.is_set():
                first_apply_started.set()
                await release_first_apply.wait()
            type(self).current_profile = profile
            return {
                "status": "applied",
                "previous_profile": previous,
                "current_profile": profile,
            }

        async def close(self):
            return None

    class CollectorEdge:
        status_calls = 0

        async def get_status(self):
            type(self).status_calls += 1
            return {
                "camera_id": "cam-001",
                "online": True,
                "capture_state": "running",
                "camera_input": "online",
                "central_connection_status": "online",
                "current_video_profile": SerializedEdge.current_profile,
                "capability_status": "available",
                "supported_profiles": ["hd", "fhd"],
                "encoder": "v4l2h264enc",
                "last_seen_at": "2026-08-23T07:20:00Z",
            }

        async def list_events(self, *, after, limit):
            del limit
            return {"items": [], "next_cursor": after}

        async def close(self):
            return None

    monkeypatch.setattr(
        "server.services.external.app.main.EdgeHttpClient", SerializedEdge
    )
    admin_token = issue_token(
        settings,
        user_id="1",
        role="admin",
        token_type="access",
        ttl_seconds=60,
    ).encoded
    headers = {"Authorization": f"Bearer {admin_token}"}
    collector = StatusCollector(
        settings=settings,
        data_client=fake_data,  # type: ignore[arg-type]
        edge_client_factory=lambda _target: CollectorEdge(),  # type: ignore[arg-type]
        camera_lock_factory=application.state.camera_lifecycle_lock_factory,
    )
    target = {
        "camera_id": "cam-001",
        "edge_device_id": "edge-001",
        "management_url": "http://edge.test:8003",
        "auth_token": "e" * 32,
        "event_cursor": None,
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application), base_url="http://test"
    ) as client:
        fhd_task = asyncio.create_task(
            client.patch(
                "/api/v1/cameras/cam-001/video-profile",
                headers=headers,
                json={"profile": "fhd"},
            )
        )
        await asyncio.wait_for(first_apply_started.wait(), timeout=1)
        collector_task = asyncio.create_task(collector._collect_target(target))
        hd_task = asyncio.create_task(
            client.patch(
                "/api/v1/cameras/cam-001/video-profile",
                headers=headers,
                json={"profile": "hd"},
            )
        )
        await asyncio.sleep(0.01)
        assert SerializedEdge.apply_calls == ["fhd"]
        assert CollectorEdge.status_calls == 0
        assert fake_data.video_profiles["cam-001"]["desired_profile"] == "fhd"
        assert not hd_task.done()

        release_first_apply.set()
        fhd_response, collection_result, hd_response = await asyncio.gather(
            fhd_task, collector_task, hd_task
        )

    assert fhd_response.status_code == 200, fhd_response.text
    assert hd_response.status_code == 200, hd_response.text
    assert SerializedEdge.apply_calls == ["fhd", "hd"]
    assert CollectorEdge.status_calls == 1
    assert collection_result == (True, 0)
    assert SerializedEdge.current_profile == "hd"
    assert fake_data.video_profiles["cam-001"]["current_profile"] == "hd"
    assert fake_data.video_profiles["cam-001"]["desired_profile"] == "hd"


def test_video_profile_does_not_duplicate_edge_journaled_rejection(
    service, monkeypatch: pytest.MonkeyPatch
):
    client, fake_data = service
    token = _login(client, "admin")["access_token"]

    class RejectedEdge:
        def __init__(self, **_kwargs):
            pass

        async def get_video_capabilities(self):
            return {
                "camera_id": "cam-001",
                "supported_profiles": ["hd", "fhd"],
                "current_profile": "hd",
                "encoder": "v4l2h264enc",
            }

        async def apply_video_profile(self, profile: str):
            raise EdgeControlError(
                "PIPELINE_START_FAILED",
                f"could not apply {profile}",
                status_code=409,
                details={
                    "status": "rejected",
                    "requested_profile": profile,
                    "reason_code": "PIPELINE_START_FAILED",
                },
                profile_outcome_journaled=True,
            )

        async def close(self):
            return None

    monkeypatch.setattr(
        "server.services.external.app.main.EdgeHttpClient", RejectedEdge
    )
    response = client.patch(
        "/api/v1/cameras/cam-001/video-profile",
        headers={"Authorization": f"Bearer {token}"},
        json={"profile": "fhd"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "PIPELINE_START_FAILED"
    assert fake_data.video_profiles["cam-001"]["current_profile"] == "hd"
    assert fake_data.video_profiles["cam-001"]["last_error_code"] == (
        "PIPELINE_START_FAILED"
    )
    assert fake_data.edge_events == []


def test_video_profile_rejects_cross_wired_edge_capability(
    service, monkeypatch: pytest.MonkeyPatch
):
    client, fake_data = service
    token = _login(client, "admin")["access_token"]

    class WrongCameraEdge:
        apply_calls = 0

        def __init__(self, **_kwargs):
            pass

        async def get_video_capabilities(self):
            return {
                "camera_id": "cam-999",
                "supported_profiles": ["hd", "fhd"],
                "current_profile": "hd",
                "encoder": "v4l2h264enc",
            }

        async def apply_video_profile(self, profile: str):
            del profile
            type(self).apply_calls += 1
            return {"status": "applied", "current_profile": "fhd"}

        async def close(self):
            return None

    monkeypatch.setattr(
        "server.services.external.app.main.EdgeHttpClient", WrongCameraEdge
    )
    response = client.patch(
        "/api/v1/cameras/cam-001/video-profile",
        headers={"Authorization": f"Bearer {token}"},
        json={"profile": "fhd"},
    )

    assert response.status_code == 502
    assert response.json()["error"]["code"] == "INVALID_EDGE_RESPONSE"
    assert WrongCameraEdge.apply_calls == 0
    assert fake_data.video_profiles["cam-001"]["current_profile"] == "hd"
    assert fake_data.edge_events[-1]["event_type"] == "video_profile_change_failed"
    assert fake_data.edge_events[-1]["metadata"]["reason_code"] == (
        "INVALID_EDGE_RESPONSE"
    )


def test_status_and_recovery_jobs_keep_camera_and_admin_acl(service):
    client, _fake_data = service
    viewer = _login(client)["access_token"]
    admin = _login(client, "admin")["access_token"]
    status_response = client.get(
        "/api/v1/cameras/cam-001/status",
        headers={"Authorization": f"Bearer {viewer}"},
    )
    assert status_response.status_code == 200
    assert status_response.json()["current_video_profile"] == "hd"
    assert (
        client.get(
            "/api/v1/recovery-jobs",
            headers={"Authorization": f"Bearer {viewer}"},
        ).status_code
        == 403
    )
    assert (
        client.get(
            "/api/v1/recovery-jobs",
            headers={"Authorization": f"Bearer {admin}"},
        ).status_code
        == 200
    )


def test_recovered_mpegts_playback_streams_with_range_and_camera_acl(
    service, monkeypatch: pytest.MonkeyPatch
):
    client, fake_data = service
    viewer = _login(client)["access_token"]
    headers = {"Authorization": f"Bearer {viewer}"}

    async def recovered_recording(segment_id: str, *, user_id: str):
        return {
            "id": segment_id,
            "camera_id": "cam-002" if segment_id == "denied" else "cam-001",
            "user_id": user_id,
            "start_time": "2026-08-22T08:00:00Z",
            "end_time": "2026-08-22T08:01:00Z",
            "format": "mpegts",
            "source": "edge_recovery",
        }

    monkeypatch.setattr(fake_data, "get_recording", recovered_recording)
    playback = client.get("/api/v1/recordings/recovered-1/playback", headers=headers)
    assert playback.status_code == 200
    assert playback.json()["playback_url"] == ("/api/v1/recordings/recovered-1/content")

    ranged = client.get(
        "/api/v1/recordings/recovered-1/content",
        headers={
            **headers,
            "Range": "bytes=2-6",
            "If-Range": '"recording-etag"',
        },
    )
    denied = client.get("/api/v1/recordings/denied/content", headers=headers)
    assert ranged.status_code == 206
    assert ranged.content == b"cover"
    assert ranged.headers["content-range"] == "bytes 2-6/17"
    assert ranged.headers["accept-ranges"] == "bytes"
    assert denied.status_code == 403
    assert fake_data.content_requests == [
        ("recovered-1", "bytes=2-6", '"recording-etag"')
    ]


def test_public_base_url_makes_media_urls_absolute(password_hash: str) -> None:
    configured = Settings(
        data_base_url="http://data.test/internal/data/v1",
        data_health_url="http://data.test/internal/data/health/ready",
        internal_token="internal-token-for-tests",
        jwt_secret="s" * 32,
        media_read_username=MEDIA_READ_USERNAME,
        media_read_password=MEDIA_READ_PASSWORD,
        cookie_secure=False,
        public_base_url="https://cctv.example.com/",
    )
    fake_data = FakeDataClient(password_hash)
    application = create_app()
    application.dependency_overrides[get_settings_dependency] = lambda: configured

    async def override_data():
        return fake_data

    application.dependency_overrides[get_data_client] = override_data
    with TestClient(application) as client:
        token = _login(client)["access_token"]
        response = client.get(
            "/api/v1/cameras/cam-001/live",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.json()["url"] == (
            "https://cctv.example.com/hls/cam-001/index.m3u8"
        )

    with pytest.raises(RuntimeError, match="HTTPS origin"):
        Settings(
            data_base_url="http://data.test/internal/data/v1",
            data_health_url="http://data.test/internal/data/health/ready",
            internal_token="internal-token-for-tests",
            jwt_secret="s" * 32,
            media_read_username=MEDIA_READ_USERNAME,
            media_read_password=MEDIA_READ_PASSWORD,
            public_base_url="https://cctv.example.com/base",
        )


@pytest.mark.asyncio
async def test_edge_http_client_auth_timeout_and_rejection_mapping() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("/capabilities/video"):
            return httpx.Response(
                200,
                json={
                    "supported_profiles": ["hd"],
                    "current_profile": "hd",
                },
            )
        return httpx.Response(
            422,
            json={
                "status": "rejected",
                "reason_code": "UNSUPPORTED_VIDEO_PROFILE",
                "message": "not supported",
            },
        )

    edge = EdgeHttpClient(
        base_url="http://edge.test:8003",
        auth_token="e" * 32,
        transport=httpx.MockTransport(handler),
    )
    assert (await edge.get_video_capabilities())["current_profile"] == "hd"
    with pytest.raises(EdgeControlError) as rejected:
        await edge.apply_video_profile("fhd")
    assert rejected.value.code == "UNSUPPORTED_VIDEO_PROFILE"
    assert rejected.value.profile_outcome_journaled is True
    assert requests[0].headers["authorization"] == f"Bearer {'e' * 32}"
    await edge.close()

    async def journaled_timeout(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            504,
            json={
                "status": "rejected",
                "reason_code": "CONTROL_TIMEOUT",
                "message": "profile transaction timed out and was rolled back",
            },
        )

    rolled_back = EdgeHttpClient(
        base_url="http://edge.test:8003",
        auth_token="e" * 32,
        transport=httpx.MockTransport(journaled_timeout),
    )
    with pytest.raises(EdgeControlError) as rejected_timeout:
        await rolled_back.apply_video_profile("fhd")
    assert rejected_timeout.value.code == "CONTROL_TIMEOUT"
    assert rejected_timeout.value.profile_outcome_journaled is True
    await rolled_back.close()

    async def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout", request=request)

    timed = EdgeHttpClient(
        base_url="http://edge.test:8003",
        auth_token="e" * 32,
        transport=httpx.MockTransport(timeout),
    )
    with pytest.raises(EdgeControlError) as timed_out:
        await timed.get_status()
    assert timed_out.value.code == "CONTROL_TIMEOUT"
    assert timed_out.value.status_code == 504
    await timed.close()


@pytest.mark.asyncio
async def test_edge_control_timeout_covers_slow_profile_apply_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "internal-token-for-tests")
    monkeypatch.setenv("DATA_EXTERNAL_TOKEN", "scoped-external-token-for-tests")
    monkeypatch.setenv("JWT_SECRET", "s" * 32)
    monkeypatch.setenv("MEDIA_READ_USERNAME", MEDIA_READ_USERNAME)
    monkeypatch.setenv("MEDIA_READ_PASSWORD", MEDIA_READ_PASSWORD)
    monkeypatch.setenv("MEDIA_PUBLISH_CREDENTIALS_JSON", "{}")
    monkeypatch.setenv("PUBLIC_BASE_URL", "")
    monkeypatch.delenv("EDGE_CONTROL_TIMEOUT_SECONDS", raising=False)
    configured = Settings.from_env()
    assert configured.internal_token == "scoped-external-token-for-tests"
    assert configured.edge_control_timeout_seconds == 75.0
    assert configured.edge_status_timeout_seconds == 5.0
    monkeypatch.delenv("DATA_EXTERNAL_TOKEN")
    assert Settings.from_env().internal_token == "internal-token-for-tests"

    observed_read_timeouts: list[float] = []

    async def slow_apply(request: httpx.Request) -> httpx.Response:
        observed_read_timeouts.append(request.extensions["timeout"]["read"])
        await asyncio.sleep(0.01)
        return httpx.Response(
            200,
            json={
                "status": "applied",
                "previous_profile": "hd",
                "current_profile": "fhd",
            },
        )

    edge = EdgeHttpClient(
        base_url="http://edge.test:8003",
        auth_token="e" * 32,
        timeout_seconds=configured.edge_control_timeout_seconds,
        transport=httpx.MockTransport(slow_apply),
    )
    response = await edge.apply_video_profile("fhd")
    await edge.close()

    assert response["status"] == "applied"
    assert observed_read_timeouts == [75.0]
    assert "EDGE_CONTROL_TIMEOUT_SECONDS=75" in Path("server/.env.example").read_text(
        encoding="utf-8"
    )
    assert "${EDGE_CONTROL_TIMEOUT_SECONDS:-75}" in Path(
        "server/compose.yml"
    ).read_text(encoding="utf-8")
    nginx_api = (
        Path("server/nginx/nginx.conf")
        .read_text(encoding="utf-8")
        .split("location ^~ /api/ {", 1)[1]
        .split("}", 1)[0]
    )
    assert "proxy_read_timeout 85s;" in nginx_api
    with pytest.raises(
        RuntimeError, match="60-second Edge lock, apply and rollback window"
    ):
        Settings(
            data_base_url="http://data.test/internal/data/v1",
            data_health_url="http://data.test/internal/data/health/ready",
            internal_token="internal-token-for-tests",
            jwt_secret="s" * 32,
            media_read_username=MEDIA_READ_USERNAME,
            media_read_password=MEDIA_READ_PASSWORD,
            edge_control_timeout_seconds=60,
        )


@pytest.mark.asyncio
async def test_status_collector_persists_cursor_and_avoids_transition_duplicates(
    settings: Settings,
) -> None:
    class CollectorData:
        def __init__(self):
            self.cursor = None
            self.events: dict[str, dict] = {}
            self.status: dict = {}
            self.profile: dict = {}

        async def list_camera_control_targets(self):
            return {
                "items": [
                    {
                        "camera_id": "cam-001",
                        "edge_device_id": "edge-001",
                        "management_url": "http://edge.test:8003",
                        "auth_token": "e" * 32,
                        "event_cursor": self.cursor,
                    }
                ]
            }

        async def put_camera_runtime_status(self, camera_id: str, payload: dict):
            previous = dict(self.status)
            self.status.update(payload)
            self.status["camera_id"] = camera_id
            if payload.get("event_cursor") is not None:
                self.cursor = payload["event_cursor"]
            return {
                **self.status,
                "previous_online": previous.get("online"),
                "previous_camera_input": previous.get("camera_input", "unknown"),
                "previous_central_connection_status": previous.get(
                    "central_connection_status", "unknown"
                ),
                "previous_power_source": previous.get("power_source", "external"),
                "previous_battery_percent": previous.get("battery_percent"),
                "previous_storage_percent": previous.get("storage_percent"),
            }

        async def get_camera_runtime_status(self, camera_id: str):
            return {
                "camera_id": camera_id,
                "online": self.status.get("online", False),
                "online_observed": self.status.get("online_observed"),
                "camera_input": self.status.get("camera_input", "unknown"),
                "central_connection_status": self.status.get(
                    "central_connection_status", "unknown"
                ),
                "power_source": self.status.get("power_source", "unknown"),
                "battery_percent": self.status.get("battery_percent"),
                "storage_percent": self.status.get("storage_percent"),
                "last_seen_at": self.status.get("last_seen_at"),
                "runtime_updated_at": self.status.get("runtime_updated_at", "initial"),
            }

        async def create_event(self, payload: dict):
            self.events.setdefault(payload["edge_event_id"], dict(payload))
            return self.events[payload["edge_event_id"]]

        async def update_camera_video_profile(self, camera_id: str, payload: dict):
            self.profile.update(payload)
            self.profile["camera_id"] = camera_id
            return dict(self.profile)

    calls: list[httpx.Request] = []

    async def edge_handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.path.endswith("/status"):
            return httpx.Response(
                200,
                json={
                    "camera_id": "cam-001",
                    "online": True,
                    "cpu_percent": 10,
                    "memory_percent": 20,
                    "storage_percent": 30,
                    "battery_percent": 80,
                    "power_source": "battery",
                    "camera_input": "online",
                    "central_connection_status": "online",
                    "current_video_profile": "hd",
                    "supported_video_profiles": ["hd", "fhd"],
                    "capability_status": "available",
                    "last_seen_at": "2026-08-23T07:20:00Z",
                },
            )
        after = request.url.params.get("after")
        items = []
        if after is None:
            items = [
                {
                    "event_id": "event-1",
                    "event_type": "external_power_lost",
                    "camera_id": "cam-001",
                    "occurred_at": "2026-08-23T07:19:59Z",
                    "battery_percent": 80,
                    "power_source": "battery",
                }
            ]
        return httpx.Response(
            200,
            json={
                "camera_id": "cam-001",
                "items": items,
                "next_cursor": "event-1" if items else after,
                "cursor_expired": False,
            },
        )

    data = CollectorData()
    transport = httpx.MockTransport(edge_handler)

    def factory(target: dict):
        return EdgeHttpClient(
            base_url=target["management_url"],
            auth_token=target["auth_token"],
            transport=transport,
        )

    collector = StatusCollector(
        settings=settings,
        data_client=data,  # type: ignore[arg-type]
        edge_client_factory=factory,
    )
    first = await collector.collect_once()
    second = await collector.collect_once()
    assert first["events_imported"] == 1
    assert second["events_imported"] == 0
    assert data.cursor == "event-1"
    assert list(data.events) == ["edge-001:event-1"]
    assert data.status["cpu_percent"] == 10
    assert data.profile["supported_profiles"] == ["hd", "fhd"]
    assert all(
        request.headers["authorization"] == f"Bearer {'e' * 32}" for request in calls
    )


@pytest.mark.asyncio
async def test_status_collector_preserves_transition_baseline_when_journal_fails(
    settings: Settings,
) -> None:
    class CollectorData:
        def __init__(self):
            self.status = {
                "online": True,
                "storage_percent": 80,
                "power_source": "external",
                "camera_input": "online",
                "central_connection_status": "online",
            }
            self.events: list[dict] = []

        async def list_camera_control_targets(self):
            return {
                "items": [
                    {
                        "camera_id": "cam-001",
                        "edge_device_id": "edge-001",
                        "management_url": "http://edge.test:8003",
                        "auth_token": "e" * 32,
                        "event_cursor": self.status.get("event_cursor"),
                    }
                ]
            }

        async def put_camera_runtime_status(self, camera_id: str, payload: dict):
            previous = dict(self.status)
            self.status.update(payload)
            self.status["camera_id"] = camera_id
            return {
                **self.status,
                "previous_online": previous.get("online"),
                "previous_camera_input": previous.get("camera_input", "unknown"),
                "previous_central_connection_status": previous.get(
                    "central_connection_status", "unknown"
                ),
                "previous_power_source": previous.get("power_source", "unknown"),
                "previous_battery_percent": previous.get("battery_percent"),
                "previous_storage_percent": previous.get("storage_percent"),
            }

        async def get_camera_runtime_status(self, camera_id: str):
            return {
                "camera_id": camera_id,
                "online_observed": True,
                "runtime_updated_at": "baseline-1",
                **self.status,
            }

        async def create_event(self, payload: dict):
            self.events.append(dict(payload))
            return payload

        async def update_camera_video_profile(self, _camera_id: str, payload: dict):
            return payload

    journal_calls = 0

    async def edge_handler(request: httpx.Request) -> httpx.Response:
        nonlocal journal_calls
        if request.url.path.endswith("/status"):
            return httpx.Response(
                200,
                json={
                    "camera_id": "cam-001",
                    "online": True,
                    "storage_percent": 90,
                    "power_source": "external",
                    "camera_input": "online",
                    "central_connection_status": "online",
                    "current_video_profile": "hd",
                    "capability_status": "available",
                    "supported_profiles": ["hd"],
                    "capture_state": "running",
                    "last_seen_at": "2026-08-23T07:20:00Z",
                },
            )
        journal_calls += 1
        if journal_calls == 1:
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "camera_id": "cam-001",
                "items": [],
                "next_cursor": None,
                "cursor_expired": False,
            },
        )

    transport = httpx.MockTransport(edge_handler)

    def factory(target: dict):
        return EdgeHttpClient(
            base_url=target["management_url"],
            auth_token=target["auth_token"],
            transport=transport,
        )

    data = CollectorData()
    collector = StatusCollector(
        settings=settings,
        data_client=data,  # type: ignore[arg-type]
        edge_client_factory=factory,
    )

    first = await collector.collect_once()
    assert first["failures"] == 1
    # The failed journal drain must not overwrite the transition baseline.
    assert data.status["storage_percent"] == 80

    second = await collector.collect_once()
    third = await collector.collect_once()
    assert second["online"] == 1
    assert third["online"] == 1
    assert [
        event["event_type"]
        for event in data.events
        if event["event_type"] == "storage_warning"
    ] == ["storage_warning"]


@pytest.mark.asyncio
async def test_status_collector_commits_events_before_baseline_and_retries_stably(
    settings: Settings,
) -> None:
    class CollectorData:
        def __init__(self):
            self.baseline = {
                "camera_id": "cam-001",
                "online": True,
                "online_observed": True,
                "storage_percent": 80,
                "power_source": "external",
                "camera_input": "online",
                "central_connection_status": "online",
                "last_seen_at": "2026-08-23T07:19:00Z",
                "runtime_updated_at": "boundary-1",
            }
            self.events: dict[str, dict] = {}
            self.event_attempt_ids: list[str] = []
            self.put_attempts = 0

        async def list_camera_control_targets(self):
            return {
                "items": [
                    {
                        "camera_id": "cam-001",
                        "edge_device_id": "edge-001",
                        "management_url": "http://edge.test:8003",
                        "auth_token": "e" * 32,
                        "event_cursor": None,
                    }
                ]
            }

        async def get_camera_runtime_status(self, _camera_id: str):
            return dict(self.baseline)

        async def update_camera_video_profile(self, _camera_id: str, payload: dict):
            return payload

        async def create_event(self, payload: dict):
            event_id = payload["edge_event_id"]
            self.event_attempt_ids.append(event_id)
            if len(self.event_attempt_ids) == 1:
                raise RuntimeError("event store unavailable")
            self.events.setdefault(event_id, dict(payload))
            return self.events[event_id]

        async def put_camera_runtime_status(self, _camera_id: str, payload: dict):
            self.put_attempts += 1
            if self.put_attempts == 1:
                raise RuntimeError("runtime store unavailable")
            self.baseline.update(payload)
            self.baseline["online_observed"] = True
            self.baseline["runtime_updated_at"] = "boundary-2"
            return dict(self.baseline)

    class CollectorEdge:
        status_calls = 0

        async def get_status(self):
            type(self).status_calls += 1
            return {
                "camera_id": "cam-001",
                "online": True,
                "storage_percent": 90,
                "power_source": "external",
                "camera_input": "online",
                "central_connection_status": "online",
                "current_video_profile": "hd",
                "capability_status": "available",
                "supported_profiles": ["hd"],
                "capture_state": "running",
                "last_seen_at": f"2026-08-23T07:20:0{type(self).status_calls}Z",
            }

        async def list_events(self, *, after, limit):
            del limit
            return {"items": [], "next_cursor": after, "cursor_expired": False}

        async def close(self):
            return None

    data = CollectorData()
    collector = StatusCollector(
        settings=settings,
        data_client=data,  # type: ignore[arg-type]
        edge_client_factory=lambda _target: CollectorEdge(),  # type: ignore[arg-type]
    )

    first = await collector.collect_once()
    assert first["failures"] == 1
    assert data.put_attempts == 0
    assert data.baseline["storage_percent"] == 80

    second = await collector.collect_once()
    assert second["failures"] == 1
    assert data.put_attempts == 1
    assert data.baseline["storage_percent"] == 80
    assert len(data.events) == 1

    third = await collector.collect_once()
    fourth = await collector.collect_once()
    assert third["online"] == 1
    assert fourth["online"] == 1
    assert data.baseline["storage_percent"] == 90
    assert len(data.events) == 1
    assert len(data.event_attempt_ids) == 3
    assert len(set(data.event_attempt_ids)) == 1


def test_status_collector_does_not_trust_unprobed_profile_declarations():
    legacy = StatusCollector._profile_observation(
        {
            "current_video_profile": "hd",
            "supported_profiles": ["hd", "fhd"],
        }
    )
    probed = StatusCollector._profile_observation(
        {
            "current_video_profile": "hd",
            "supported_profiles": ["hd"],
            "capability_status": "available",
        }
    )

    assert legacy == {"current_profile": "hd"}
    assert probed == {"current_profile": "hd", "supported_profiles": ["hd"]}


@pytest.mark.asyncio
async def test_status_collector_ignores_unknown_transition_states(
    settings: Settings,
) -> None:
    class TransitionData:
        def __init__(self):
            self.events: list[dict] = []

        async def create_event(self, payload: dict):
            self.events.append(dict(payload))
            return payload

    data = TransitionData()
    collector = StatusCollector(
        settings=settings,
        data_client=data,  # type: ignore[arg-type]
    )
    await collector._synthesise_missing_transitions(
        "cam-001",
        {
            "_capture_state": "unknown",
            "last_seen_at": "2026-08-23T07:20:00Z",
            "camera_input": "unknown",
            "central_connection_status": "unknown",
        },
        {
            "previous_camera_input": "online",
            "previous_central_connection_status": "online",
        },
        set(),
    )
    await collector._synthesise_missing_transitions(
        "cam-001",
        {
            "_capture_state": "unknown",
            "last_seen_at": "2026-08-23T07:20:01Z",
            "camera_input": "offline",
            "central_connection_status": "offline",
        },
        {
            "previous_camera_input": "unknown",
            "previous_central_connection_status": "unknown",
        },
        set(),
    )
    await collector._synthesise_missing_transitions(
        "cam-001",
        {
            "_capture_state": "stopped",
            "last_seen_at": "2026-08-23T07:20:01Z",
            "camera_input": "offline",
            "central_connection_status": "offline",
        },
        {
            "previous_camera_input": "online",
            "previous_central_connection_status": "online",
        },
        set(),
    )
    await collector._synthesise_missing_transitions(
        "cam-001",
        {
            "_capture_state": "running",
            "last_seen_at": "2026-08-23T07:20:01Z",
            "camera_input": "online",
            "central_connection_status": "online",
        },
        {
            "previous_camera_input": "unknown",
            "previous_central_connection_status": "unknown",
        },
        set(),
    )
    assert data.events == []

    await collector._synthesise_missing_transitions(
        "cam-001",
        {
            "_capture_state": "running",
            "last_seen_at": "2026-08-23T07:20:02Z",
            "camera_input": "lost",
            "central_connection_status": "offline",
        },
        {
            "previous_camera_input": "online",
            "previous_central_connection_status": "online",
        },
        set(),
    )
    await collector._synthesise_missing_transitions(
        "cam-001",
        {
            "_capture_state": "running",
            "last_seen_at": "2026-08-23T07:20:03Z",
            "camera_input": "online",
            "central_connection_status": "online",
        },
        {
            "previous_camera_input": "offline",
            "previous_central_connection_status": "offline",
        },
        set(),
    )
    assert [event["event_type"] for event in data.events] == [
        "camera_input_lost",
        "central_connection_lost",
        "camera_input_restored",
        "central_connection_restored",
    ]
