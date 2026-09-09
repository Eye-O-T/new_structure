import httpx
import pytest

from server.services.external.app.clients.data import DataClient, DataInvalidRequest
from server.services.external.app.dependencies import get_data_client
from server.services.external.app.main import create_app
from server.services.external.app.security.tokens import issue_token
from server.services.external.tests.test_external_service import (
    FakeDataClient,
    settings,
)


@pytest.mark.asyncio
async def test_public_events_forwards_cursor_and_preserves_page_boundary():
    config = settings.__wrapped__()
    data = FakeDataClient("unused")
    requests = []

    async def page(**params):
        requests.append(params)
        return {
            "items": [],
            "limit": 50,
            "offset": 0,
            "snapshot_max_id": 99,
            "has_more": True,
            "next_cursor": "next-page",
        }

    data.list_events = page
    app = create_app(settings=config)
    app.dependency_overrides[get_data_client] = lambda: data
    token = issue_token(
        config,
        user_id="2",
        role="viewer",
        token_type="access",
        session_id="test-session-2",
        ttl_seconds=60,
    ).encoded
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://external.test"
    ) as client:
        response = await client.get(
            "/api/v1/events",
            headers={"Authorization": f"Bearer {token}"},
            params={"camera_id": "cam-001", "cursor": "first-page", "order": "desc"},
        )
    assert response.status_code == 200, response.text
    assert response.json()["snapshot_max_id"] == 99
    assert response.json()["next_cursor"] == "next-page"
    assert requests[0]["cursor"] == "first-page"
    assert requests[0]["order"] == "desc"
    assert data.permission_calls == ["2"]


@pytest.mark.asyncio
async def test_data_http_event_request_contains_cursor_and_direction():
    def handler(request):
        assert request.url.params["cursor"] == "page-token"
        assert request.url.params["order"] == "desc"
        return httpx.Response(
            200, json={"items": [], "next_cursor": None, "has_more": False}
        )

    client = DataClient(
        base_url="http://data.test/internal/v1",
        health_url="http://data.test/health/ready",
        internal_token="internal-test-token",
        transport=httpx.MockTransport(handler),
    )
    try:
        assert (await client.list_events(cursor="page-token", order="desc"))[
            "has_more"
        ] is False
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_invalid_cursor_is_a_public_input_error_without_upstream_details():
    def handler(request):
        return httpx.Response(
            422,
            json={
                "error": {
                    "code": "INVALID_EVENT_CURSOR",
                    "message": "private upstream error",
                }
            },
        )

    client = DataClient(
        base_url="http://data.test/internal/v1",
        health_url="http://data.test/health/ready",
        internal_token="internal-test-token",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(DataInvalidRequest) as failure:
            await client.list_events(cursor="malformed")
        assert failure.value.code == "INVALID_EVENT_CURSOR"
        assert "private" not in failure.value.message
    finally:
        await client.close()
