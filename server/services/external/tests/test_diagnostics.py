import asyncio
from dataclasses import replace

import httpx
import pytest

from server.services.external.app.api import health
from server.services.external.app.clients.data import DataClient
from server.services.external.app.dependencies import get_data_client
from server.services.external.app.diagnostics import system_diagnostics
from server.services.external.app.diagnostics import public_data_status
from server.services.external.app.main import create_app
from server.services.external.app.notifications.firebase import PushSendError
from server.services.external.app.security.tokens import issue_token
from server.services.external.app.workers.push_dispatcher import PushDispatcher
from server.services.external.tests.test_external_service import FakeDataClient
from server.services.external.tests.test_external_service import (
    settings as shared_settings,
)


@pytest.fixture
def settings():
    return shared_settings.__wrapped__()


def test_disk_warning_and_deferred_cleanup_are_visible_even_with_data_ready():
    for details in (
        {
            "storage": {
                "volumes": {"snapshots": {"status": "warning", "free_percent": 2}}
            }
        },
        {"retention": {"status": "deferred", "reason": "private-path"}},
    ):
        result = public_data_status({"status": "ready", **details})
        assert result["status"] == "degraded"
        assert "private-path" not in str(result)


@pytest.mark.asyncio
async def test_diagnostics_preserves_degradation_and_strips_private_fields(settings):
    class Data:
        async def health_status(self):
            return {
                "status": "degraded",
                "private_token": "never-public",
                "workers": {
                    "recovery": {
                        "alive": False,
                        "status": "error",
                        "last_error": "secret-path",
                        "last_success_at": "2026-09-09T00:00:00Z",
                    }
                },
                "storage": {
                    "volumes": {
                        "snapshots": {
                            "status": "warning",
                            "free_percent": 2.5,
                            "path": "secret-path",
                        }
                    }
                },
                "queues": {"push": {"pending": 7, "token": "never-public"}},
                "queue_metrics": {
                    "push": {
                        "oldest_pending_seconds": 123,
                        "last_success_at": "2026-09-09T00:00:00Z",
                        "last_failure_at": "2026-09-09T00:01:00Z",
                        "last_error": "secret-path",
                    }
                },
            }

    def handler(request):
        assert "x-internal-token" not in request.headers
        assert "authorization" not in request.headers
        if "paths/list" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "itemCount": 1,
                    "items": [
                        {
                            "ready": True,
                            "source": {"id": "secret-session", "type": "rtspSession"},
                        }
                    ],
                },
            )
        return httpx.Response(
            200,
            json={
                "status": "degraded",
                "data_ready": True,
                "workers": {
                    "cam-001": {
                        "state": "online",
                        "model_ready": True,
                        "frame_stale": True,
                        "frame_age_seconds": 90,
                        "last_error": "rtsp://private:password@edge",
                    }
                },
                "identity": {
                    "ready": True,
                    "model_ready": False,
                    "last_error": "secret-path",
                },
                "event_delivery": {
                    "pending": 4,
                    "rejected": 2,
                    "waiting": 3,
                    "last_error": "secret-path",
                },
            },
        )

    result = await system_diagnostics(
        settings, Data(), transport=httpx.MockTransport(handler)
    )
    assert result["data"]["status"] == "degraded"
    assert result["data"]["workers"]["recovery"]["last_error_code"] == "WORKER_ERROR"
    assert result["data"]["queues"]["push"]["pending"] == 7
    assert result["data"]["queue_metrics"]["push"]["oldest_pending_seconds"] == 123
    assert result["data"]["queue_metrics"]["push"]["last_failure_at"]
    assert result["preprocessing"]["cameras"][0]["frame_stale"] is True
    assert result["preprocessing"]["event_delivery"]["rejected"] == 2
    assert result["preprocessing"]["event_delivery"]["waiting"] == 3
    assert result["media"]["active_publishers"] == 1
    serialized = str(result)
    for secret in (
        "secret-path",
        "secret-session",
        "never-public",
        "rtsp://",
        "password",
    ):
        assert secret not in serialized


@pytest.mark.asyncio
async def test_slow_probe_does_not_hide_other_service_diagnostics(settings):
    class Data:
        async def health_status(self):
            return {"status": "ready"}

    async def handler(request):
        if "paths/list" in request.url.path:
            return httpx.Response(200, json={"itemCount": 0, "items": []})
        await asyncio.Event().wait()

    result = await asyncio.wait_for(
        system_diagnostics(
            replace(settings, system_probe_timeout_seconds=0.05),
            Data(),
            transport=httpx.MockTransport(handler),
        ),
        timeout=1,
    )
    assert result["data"]["status"] == "ready"
    assert result["media"]["status"] == "ready"
    assert result["preprocessing"] == {
        "status": "unavailable",
        "last_error_code": "PREPROCESSING_UNAVAILABLE",
    }


@pytest.mark.asyncio
async def test_data_health_details_survive_worker_503(settings):
    def handler(request):
        assert request.headers["x-internal-token"] == settings.internal_token
        return httpx.Response(
            503, json={"status": "degraded", "workers": {"storage": {"alive": False}}}
        )

    client = DataClient(
        base_url=settings.data_base_url,
        health_url=settings.data_health_url,
        internal_token=settings.internal_token,
        transport=httpx.MockTransport(handler),
    )
    try:
        assert (await client.health_status())["workers"]["storage"]["alive"] is False
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_admin_receives_all_degraded_components_and_viewer_cannot_probe(
    settings, monkeypatch
):
    data = FakeDataClient("unused")
    app = create_app(settings=settings)
    app.dependency_overrides[get_data_client] = lambda: data
    calls = []

    async def diagnostics(_settings, _data):
        calls.append(True)
        return {
            "data": {"status": "ready"},
            "preprocessing": {"status": "degraded"},
            "media": {"status": "unavailable", "last_error_code": "MEDIA_UNAVAILABLE"},
        }

    monkeypatch.setattr(health, "system_diagnostics", diagnostics)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://external.test"
    ) as client:
        for user, role, expected in (("2", "viewer", 403), ("1", "admin", 200)):
            token = issue_token(
                settings,
                user_id=user,
                role=role,
                token_type="access",
                session_id=f"test-session-{user}",
                ttl_seconds=60,
            ).encoded
            response = await client.get(
                "/api/v1/admin/system/status",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert response.status_code == expected
        assert response.json()["status"] == "degraded"
        assert response.json()["push"]["status"] == "disabled"
        assert response.headers["cache-control"] == "no-store"
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_push_diagnostics_distinguishes_waiting_failure_and_confirmed_delivery(
    settings,
):
    class Data:
        async def claim_push(self):
            return {"id": 1}

        async def complete_push(self, *args):
            pass

    class Sender:
        fail = True

        async def send(self, delivery):
            if self.fail:
                raise PushSendError("retry", "private-error")

    sender = Sender()
    dispatcher = PushDispatcher(settings, Data(), sender)
    dispatcher.running = True
    assert dispatcher.status()["status"] == "waiting"
    await dispatcher.dispatch_once()
    assert dispatcher.status()["status"] == "degraded"
    assert dispatcher.status()["last_error_code"] == "PUSH_DELIVERY_FAILED"
    sender.fail = False
    await dispatcher.dispatch_once()
    assert dispatcher.status()["status"] == "ready"
    assert dispatcher.status()["delivery_confirmed"] is True
    assert dispatcher.status()["last_sent_at"]
