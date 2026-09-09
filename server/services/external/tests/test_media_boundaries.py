"""External → Edge 설정 → External 송출 인증의 실제 HTTP 라우터 경계를 검증한다."""

import asyncio
from dataclasses import replace

import httpx
import pytest

from ai_cctv_edge.control import (
    LocalProfileRuntime,
    VideoCapabilities,
    create_control_app,
)
from ai_cctv_edge.state import ProfileSelectionStore, RuntimeStatusStore
from edge.tests.test_edge import write_config
from server.services.external.app.api import cameras
from server.services.external.app.clients.edge import EdgeHttpClient
from server.services.external.app.dependencies import get_data_client
from server.services.external.app.main import create_app
from server.services.external.app.security.tokens import issue_token
from server.services.external.tests.test_external_service import FakeDataClient
from server.services.external.tests.test_external_service import (
    settings as shared_settings,
)


@pytest.fixture
def settings():
    return shared_settings.__wrapped__()


@pytest.mark.asyncio
async def test_missing_publish_credential_rejects_empty_credentials(settings, caplog):
    data = FakeDataClient("unused")
    app = create_app(settings=replace(settings, media_publish_credentials={}))
    app.dependency_overrides[get_data_client] = lambda: data
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://external.test"
    ) as client:
        response = await client.post(
            "/internal/media-auth",
            json={"action": "publish", "protocol": "rtsp", "path": "cam-001"},
        )
    assert response.status_code == 401
    assert "PUBLISH_CREDENTIAL_MISSING camera_id=cam-001" in caplog.text
    assert "PUBLISH_CREDENTIAL_MISSING" not in response.text


@pytest.mark.asyncio
async def test_profile_reauthentication_completes_before_queued_disable(
    settings, tmp_path, monkeypatch
):
    # 하드웨어 캡처만 대체한다. 중앙/Edge 라우터, HTTP 클라이언트, 프로필 manager와
    # LocalProfileRuntime의 실제 healthy 판정 및 송출 인증/비활성화 코드를 실행한다.
    from ai_cctv_edge.config import EdgeConfig

    loop = asyncio.get_running_loop()
    data = FakeDataClient("unused")
    central_app = create_app(settings=settings)
    central_app.dependency_overrides[get_data_client] = lambda: data
    config_path = tmp_path / "config.toml"
    write_config(config_path, mode="central_publish")
    (tmp_path / "recovery.token").write_text("e" * 32, encoding="utf-8")
    config = EdgeConfig.load(config_path)
    selection = ProfileSelectionStore(tmp_path / "state")
    status_store = RuntimeStatusStore(tmp_path / "state")
    publisher_authenticated = asyncio.Event()
    finish_apply = asyncio.Event()

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=central_app), base_url="http://external.test"
    ) as central:

        async def reconnect_publisher():
            response = await central.post(
                "/internal/media-auth",
                json={
                    "action": "publish",
                    "protocol": "rtsp",
                    "path": "cam-001",
                    "user": "publisher",
                    "password": "camera-secret",
                },
            )
            assert response.status_code == 204
            publisher_authenticated.set()
            await finish_apply.wait()

        class CaptureRuntime(LocalProfileRuntime):
            def preflight(self, candidate, timeout_seconds):
                pass

            def activate(self, profile, generation):
                status_store.write(
                    {
                        "state": "running",
                        "camera_input": "online",
                        "current_video_profile": profile,
                        "profile_generation": generation,
                        "central_connection_status": "connecting",
                    }
                )

            def wait_for(self, profile, generation, timeout_seconds):
                future = asyncio.run_coroutine_threadsafe(reconnect_publisher(), loop)
                try:
                    future.result(timeout=3)
                except BaseException:
                    future.cancel()
                    raise
                status_store.write(
                    {
                        **status_store.read(),
                        "central_connection_status": "online",
                    }
                )
                return super().wait_for(profile, generation, timeout_seconds)

            def commit(self, profile, generation):
                selection.write(profile, generation)

        class Probe:
            def inspect(self, _config):
                return VideoCapabilities(("hd", "fhd"), True, True)

        edge_app = create_control_app(
            config_path,
            state_root=tmp_path / "state",
            capability_probe=Probe(),
            profile_runtime=CaptureRuntime(
                config, selection, status_store, tmp_path / "run"
            ),
        )

        def edge_client(**kwargs):
            return EdgeHttpClient(**kwargs, transport=httpx.ASGITransport(app=edge_app))

        async def disconnect(_settings, camera_id):
            data.disconnected_publishers.append(camera_id)
            return True

        monkeypatch.setattr(cameras, "EdgeHttpClient", edge_client)
        monkeypatch.setattr(cameras, "_disconnect_camera_publisher", disconnect)
        token = issue_token(
            settings,
            user_id="1",
            role="admin",
            token_type="access",
            session_id="test-session-1",
            ttl_seconds=60,
        ).encoded
        headers = {"Authorization": f"Bearer {token}"}
        profile_task = asyncio.create_task(
            central.patch(
                "/api/v1/cameras/cam-001/video-profile",
                headers=headers,
                json={"profile": "fhd"},
            )
        )
        disable_task = None
        try:
            await asyncio.wait_for(publisher_authenticated.wait(), timeout=2)
            disable_task = asyncio.create_task(
                central.patch(
                    "/api/v1/cameras/cam-001",
                    headers=headers,
                    json={"enabled": False},
                )
            )
            await asyncio.sleep(0.01)
            assert not disable_task.done()
            finish_apply.set()
            changed, disabled = await asyncio.wait_for(
                asyncio.gather(profile_task, disable_task), timeout=3
            )
            assert changed.status_code == 200, changed.text
            assert changed.json()["current_profile"] == "fhd"
            assert selection.read("hd")[0] == "fhd"
            assert disabled.status_code == 200
            assert data.camera_enabled["cam-001"] is False
            assert data.disconnected_publishers == ["cam-001"]
        finally:
            finish_apply.set()
            for task in (profile_task, disable_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (profile_task, disable_task) if task is not None),
                return_exceptions=True,
            )
