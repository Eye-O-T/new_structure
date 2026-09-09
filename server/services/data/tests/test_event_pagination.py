from datetime import UTC, datetime, timedelta

import pytest

from server.services.data.app.database.repositories import events as event_repository
from server.services.data.tests.test_data_service import BASE, HEADERS, _create_camera
from server.services.data.tests.test_data_service import data_client as shared_client


@pytest.fixture
def data_client(tmp_path):
    yield from shared_client.__wrapped__(tmp_path)


def _event(client, seconds):
    response = client.post(
        f"{BASE}/events",
        headers=HEADERS,
        json={
            "camera_id": "cam-001",
            "event_type": "person_detected",
            "occurred_at": (
                datetime(2026, 9, 9, tzinfo=UTC) + timedelta(seconds=seconds)
            ).isoformat(),
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.mark.parametrize("order", ["asc", "desc"])
def test_cursor_pages_freeze_insert_boundary_and_handle_equal_timestamps(
    data_client, order
):
    client, _ = data_client
    _create_camera(client, "cam-001")
    events = [_event(client, seconds) for seconds in (0, 1, 1, 2, 3, 4, 5)]
    query = {"camera_id": "cam-001", "limit": 2, "order": order}
    first = client.get(f"{BASE}/events", headers=HEADERS, params=query).json()
    boundary = first["snapshot_max_id"]
    assert first["has_more"] is True
    # 조회 중 지연 수신된 과거 이벤트와 최신 이벤트 모두 다음 새 조회에서만 보여야 한다.
    for seconds in (-1, 1.5, 9):
        assert _event(client, seconds)["id"] > boundary
    seen = [item["id"] for item in first["items"]]
    page = first
    while page["next_cursor"] is not None:
        response = client.get(
            f"{BASE}/events",
            headers=HEADERS,
            params={**query, "cursor": page["next_cursor"]},
        )
        assert response.status_code == 200, response.text
        page = response.json()
        assert page["snapshot_max_id"] == boundary
        seen.extend(item["id"] for item in page["items"])
    expected = [event["id"] for event in events]
    assert seen == (expected if order == "asc" else list(reversed(expected)))
    assert page["has_more"] is False
    newest = client.get(
        f"{BASE}/events", headers=HEADERS, params={"camera_id": "cam-001", "limit": 100}
    ).json()
    assert len(newest["items"]) == 10


def test_invalid_or_reused_cursor_cannot_silently_change_query(data_client):
    client, _ = data_client
    _create_camera(client, "cam-001")
    _event(client, 0)
    _event(client, 1)
    query = {"camera_id": "cam-001", "limit": 1}
    cursor = client.get(f"{BASE}/events", headers=HEADERS, params=query).json()[
        "next_cursor"
    ]
    for patch in (
        {"cursor": cursor, "order": "desc"},
        {"cursor": cursor, "offset": 1},
        {"cursor": cursor, "event_type": "person_appeared"},
        {"cursor": "malformed!"},
    ):
        response = client.get(
            f"{BASE}/events", headers=HEADERS, params={**query, **patch}
        )
        assert response.status_code == 422, response.text


def test_page_remains_consistent_when_retention_deletes_selected_events(
    data_client, monkeypatch
):
    client, _ = data_client
    _create_camera(client, "cam-001")
    expected = [_event(client, seconds)["id"] for seconds in (0, 1)]
    database = client.app.state.repository.database
    original_event = event_repository._event
    deleted = False

    def delete_after_page_select(row):
        nonlocal deleted
        if not deleted:
            deleted = True
            # 별도 SQLite 쓰기 연결에서 조회와 겹치는 실제 보존 삭제를 수행한다.
            with database.transaction() as connection:
                connection.execute("DELETE FROM events")
        return original_event(row)

    monkeypatch.setattr(event_repository, "_event", delete_after_page_select)
    response = client.get(
        f"{BASE}/events", headers=HEADERS, params={"camera_id": "cam-001"}
    )
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == expected
    assert deleted
    assert client.get(f"{BASE}/events", headers=HEADERS).json()["items"] == []
