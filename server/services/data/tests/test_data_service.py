# 실제 SQLite와 임시 파일을 사용하는 Data API 통합 테스트: 인증·시간 경계·저장 일관성·복구를 검사한다.
from __future__ import annotations

import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from server.services.data.app.config import Settings
from server.services.data.app.main import create_app

TOKEN = "test-internal-token"
HEADERS = {"X-Internal-Token": TOKEN}
BASE = "/internal/v1"
SCOPED_TOKENS = {
    "external": "e" * 40,
    "inference": "i" * 40,
    "media": "m" * 40,
    "recovery": "r" * 40,
}


# 임시 DB·녹화 폴더와 실제 앱 수명 주기를 사용하여 저장 계층까지 함께 검증한다.
@pytest.fixture
def data_client(tmp_path: Path):
    settings = Settings(
        database_path=tmp_path / "database" / "ai_cctv.db",
        storage_root=tmp_path / "recordings",
        snapshot_root=tmp_path / "snapshots",
        backup_root=tmp_path / "backups",
        internal_token=TOKEN,
        busy_timeout_ms=750,
    )
    app = create_app(settings=settings)
    with TestClient(app) as client:
        yield client, settings


# 공통 토큰 없이 서로 다른 네 서비스 토큰을 주입하여 실제 라우트별 권한을 검사한다.
@pytest.fixture
def scoped_data_client(tmp_path: Path):
    settings = Settings(
        database_path=tmp_path / "database" / "ai_cctv.db",
        storage_root=tmp_path / "recordings",
        snapshot_root=tmp_path / "snapshots",
        backup_root=tmp_path / "backups",
        internal_token="",
        data_external_token=SCOPED_TOKENS["external"],
        data_inference_token=SCOPED_TOKENS["inference"],
        data_media_token=SCOPED_TOKENS["media"],
        data_recovery_token=SCOPED_TOKENS["recovery"],
        busy_timeout_ms=750,
    )
    app = create_app(settings=settings)
    with TestClient(app) as client:
        yield client


# 서비스 토큰별 허용·금지 라우트와 잘못된 토큰의 미인증 응답을 검증한다.
def test_scoped_internal_tokens_enforce_least_privilege(scoped_data_client):
    client = scoped_data_client
    headers = {
        scope: {"X-Internal-Token": token} for scope, token in SCOPED_TOKENS.items()
    }

    assert client.get(f"{BASE}/users", headers=headers["external"]).status_code == 200
    assert client.get(f"{BASE}/users", headers=headers["inference"]).status_code == 403
    assert (
        client.get(f"{BASE}/cameras/enabled", headers=headers["inference"]).status_code
        == 200
    )
    assert (
        client.get(f"{BASE}/cameras/enabled", headers=headers["external"]).status_code
        == 403
    )
    assert (
        client.patch(
            f"{BASE}/cameras/missing/status",
            headers=headers["inference"],
            json={"status": "online"},
        ).status_code
        == 404
    )
    assert (
        client.patch(
            f"{BASE}/cameras/missing/status",
            headers=headers["external"],
            json={"status": "online"},
        ).status_code
        == 403
    )
    assert (
        client.post(f"{BASE}/events", headers=headers["inference"], json={}).status_code
        == 422
    )
    assert (
        client.post(f"{BASE}/events", headers=headers["media"], json={}).status_code
        == 403
    )
    assert (
        client.post(
            f"{BASE}/hooks/recording-complete", headers=headers["media"], json={}
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"{BASE}/hooks/recording-complete",
            headers=headers["external"],
            json={},
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"{BASE}/recording-segments", headers=headers["recovery"], json={}
        ).status_code
        == 422
    )
    assert (
        client.post(
            f"{BASE}/recording-segments", headers=headers["media"], json={}
        ).status_code
        == 403
    )
    assert (
        client.get(
            f"{BASE}/users", headers={"X-Internal-Token": "unknown-token"}
        ).status_code
        == 401
    )


# 서비스별 토큰이 일부만 설정되면 비어 있는 권한이 공통 토큰으로 열리지 않아야 한다.
def test_partial_scoped_tokens_cannot_fall_back_to_legacy(tmp_path: Path) -> None:
    settings = Settings(
        database_path=tmp_path / "database" / "ai_cctv.db",
        storage_root=tmp_path / "recordings",
        snapshot_root=tmp_path / "snapshots",
        backup_root=tmp_path / "backups",
        internal_token=TOKEN,
        data_external_token=SCOPED_TOKENS["external"],
    )
    assert settings.data_api_tokens()["inference"] == ""
    with pytest.raises(ValueError, match="must be configured together"):
        settings.prepare_directories()


# 중앙 녹화 조각 길이는 설정한 지원 범위 안에서만 허용되어야 한다.
@pytest.mark.parametrize("seconds", (9, 301))
def test_data_settings_reject_segment_duration_outside_srs_range(
    tmp_path: Path, seconds: int
) -> None:
    settings = Settings(
        database_path=tmp_path / "database" / "ai_cctv.db",
        storage_root=tmp_path / "recordings",
        snapshot_root=tmp_path / "snapshots",
        backup_root=tmp_path / "backups",
        internal_token=TOKEN,
        central_recording_segment_seconds=seconds,
    )

    with pytest.raises(ValueError, match="range 10..300"):
        settings.prepare_directories()


# 서비스별 토큰을 사용하지 않는 기존 배포에서는 공통 토큰 호환성을 유지한다.
def test_data_settings_preserve_legacy_runtime_token_fallback(monkeypatch) -> None:
    for name in (
        "DATA_EXTERNAL_TOKEN",
        "DATA_INFERENCE_TOKEN",
        "DATA_IDENTITY_TOKEN",
        "DATA_ANALYSIS_TOKEN",
        "DATA_MEDIA_TOKEN",
        "DATA_RECOVERY_TOKEN",
        "DATA_INTERNAL_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", TOKEN)
    settings = Settings.from_env()
    assert set(settings.data_api_tokens().values()) == {TOKEN}


# 내부 API로 사용자를 생성해 각 시나리오가 실제 저장 검증을 거친 계정을 사용하게 한다.
def _create_user(client: TestClient, username: str, role: str = "viewer") -> dict:
    response = client.post(
        f"{BASE}/users",
        headers=HEADERS,
        json={
            "username": username,
            "password_hash": f"hash-for-{username}",
            "role": role,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


# 카메라 생성 성공을 보장하는 공통 준비 함수이다.
def _create_camera(client: TestClient, camera_id: str) -> dict:
    response = client.post(
        f"{BASE}/cameras",
        headers=HEADERS,
        json={
            "camera_id": camera_id,
            "name": camera_id,
            "stream_path": camera_id,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


# 임시 저장소에 실제 파일을 만든 뒤 등록하여 파일 통계 검증도 함께 실행한다.
def _create_segment(
    client: TestClient,
    settings: Settings,
    *,
    camera_id: str = "cam-001",
    relative_path: str,
    start: datetime,
    end: datetime,
    idempotency_key: str | None = None,
) -> dict:
    target = settings.storage_root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"video-segment")
    response = client.post(
        f"{BASE}/recording-segments",
        headers=HEADERS,
        json={
            "camera_id": camera_id,
            "start_time": start.isoformat(),
            "end_time": end.isoformat(),
            "relative_path": relative_path,
            "format": "mp4",
            "source": "central",
            "idempotency_key": idempotency_key,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


# 실제 DB 초기화 결과의 인덱스·WAL·외래 키와 준비 상태 응답을 확인한다.
def test_schema_indexes_pragmas_and_foreign_keys(data_client) -> None:
    client, settings = data_client
    assert client.get("/health/live").status_code == 200
    ready = client.get("/health/ready")
    assert ready.status_code == 200
    assert ready.json()["database"]["journal_mode"] == "wal"
    assert ready.json()["database"]["foreign_keys"] is True
    assert ready.json()["storage"]["free_bytes"] > 0

    with sqlite3.connect(settings.database_path) as connection:
        indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        assert "idx_segments_camera_time" in indexes
        assert "idx_events_camera_time_type" in indexes
        assert "idx_event_segments_segment" in indexes

    _create_camera(client, "cam-001")
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO recording_segments(
                    camera_id, start_time, end_time, relative_path, format, codec,
                    duration_ms, file_size, source, status, created_at, updated_at
                ) VALUES ('missing-camera', '2026-01-01T00:00:00.000Z',
                    '2026-01-01T00:01:00.000Z', 'x.mp4', 'mp4', 'h264',
                    60000, 1, 'central', 'ready',
                    '2026-01-01T00:00:00.000Z', '2026-01-01T00:00:00.000Z')
                """
            )


# 인증 없는 내부 요청은 공통 JSON 오류 구조의 401로 거절되어야 한다.
def test_internal_routes_require_token_and_use_json_error_shape(data_client) -> None:
    client, _settings = data_client
    response = client.get(f"{BASE}/cameras")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_INTERNAL_TOKEN"


# 시간 경계가 맞닿기만 하는 녹화는 제외하고 겹친 결과의 페이지 순서를 유지한다.
def test_overlap_boundaries_and_pagination(data_client) -> None:
    client, settings = data_client
    _create_camera(client, "cam-001")
    base = datetime(2026, 8, 22, tzinfo=UTC)
    first = _create_segment(
        client,
        settings,
        relative_path="cam-001/first.mp4",
        start=base,
        end=base + timedelta(seconds=60),
    )
    second = _create_segment(
        client,
        settings,
        relative_path="cam-001/second.mp4",
        start=base + timedelta(seconds=60),
        end=base + timedelta(seconds=120),
    )

    response = client.get(
        f"{BASE}/recording-segments/search",
        headers=HEADERS,
        params={
            "camera_id": "cam-001",
            "from": (base + timedelta(seconds=60)).isoformat(),
            "to": (base + timedelta(seconds=61)).isoformat(),
            "limit": 1,
            "offset": 0,
        },
    )
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == [second["id"]]
    assert first["id"] != second["id"]
    assert response.json()["limit"] == 1


# 같은 파일의 재등록은 같은 ID를 반환하고 크기는 실제 파일 통계를 따라야 한다.
def test_segment_idempotency_and_file_stat(data_client) -> None:
    client, settings = data_client
    _create_camera(client, "cam-001")
    start = datetime(2026, 8, 22, tzinfo=UTC)
    first = _create_segment(
        client,
        settings,
        relative_path="cam-001/idempotent.mp4",
        start=start,
        end=start + timedelta(seconds=60),
        idempotency_key="same-hook",
    )
    second = _create_segment(
        client,
        settings,
        relative_path="cam-001/idempotent.mp4",
        start=start,
        end=start + timedelta(seconds=60),
        idempotency_key="same-hook",
    )
    assert first["id"] == second["id"]
    assert first["file_size"] == len(b"video-segment")
    assert second["idempotent_replay"] is True


# 완료 훅의 반복 호출이 파일 기반 메타데이터와 단일 녹화 행으로 수렴하는지 확인한다.
@pytest.mark.parametrize(
    "filename",
    [
        "hook.mp4",
        "20260230T040308-227398Z.mp4",
        "20260908T040308-Z.mp4",
    ],
)
def test_recording_complete_hook_derives_metadata_and_is_idempotent(
    data_client, filename,
) -> None:
    client, settings = data_client
    _create_camera(client, "cam-001")
    target = settings.storage_root / "cam-001" / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"hook-video")
    expected_end = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    os.utime(target, (expected_end.timestamp(), expected_end.timestamp()))
    form = {
        "camera_id": "cam-001",
        "segment_path": str(target),
        "duration_seconds": "10",
    }
    first = client.post(f"{BASE}/hooks/recording-complete", headers=HEADERS, data=form)
    second = client.post(f"{BASE}/hooks/recording-complete", headers=HEADERS, data=form)
    assert first.status_code == 201, first.text
    assert second.status_code == 201, second.text
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["relative_path"] == f"cam-001/{filename}"
    assert first.json()["file_size"] == len(b"hook-video")
    assert first.json()["duration_ms"] == 10_000
    assert first.json()["end_time"] == "2026-08-22T12:00:00.000Z"
    assert first.json()["start_time"] == "2026-08-22T11:59:50.000Z"
    assert second.json()["idempotent_replay"] is True


# 파일 쓰기가 늦어져도 녹화 시작 시각과 소수 초는 파일명에서 읽어야 한다.
@pytest.mark.parametrize(
    "fraction,expected_fraction",
    [("227398", "227"), ("2", "200"), ("227398999", "227")],
)
def test_recording_hook_uses_filename_start_instead_of_file_write_time(
    data_client, fraction, expected_fraction,
) -> None:
    client, settings = data_client
    _create_camera(client, "cam-001")
    target = (
        settings.storage_root
        / f"cam-001/2026/09/08/20260908T040308-{fraction}Z.mp4"
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"completed-central-recording")
    # 실제 파일처럼 수정 시각과 '파일명 시작 + 영상 길이'가 어긋나게 한다.
    modified = datetime(2026, 9, 8, 4, 3, 18, 92901, tzinfo=UTC).timestamp()
    os.utime(target, (modified, modified))

    response = client.post(
        f"{BASE}/hooks/recording-complete",
        headers=HEADERS,
        data={
            "camera_id": "cam-001",
            "segment_path": str(target),
            "duration_seconds": "10",
        },
    )

    assert response.status_code == 201, response.text
    assert response.json()["start_time"] == f"2026-09-08T04:03:08.{expected_fraction}Z"
    assert response.json()["end_time"] == f"2026-09-08T04:03:18.{expected_fraction}Z"
    assert response.json()["duration_ms"] == 10_000


# 대표 녹화와 전체 녹화 연결 및 로컬·전역 인물 ID가 함께 보존되는지 검사한다.
def test_event_has_primary_and_many_to_many_segment_links(data_client) -> None:
    client, settings = data_client
    _create_camera(client, "cam-001")
    base = datetime(2026, 8, 22, tzinfo=UTC)
    first = _create_segment(
        client,
        settings,
        relative_path="cam-001/a.mp4",
        start=base,
        end=base + timedelta(seconds=60),
    )
    second = _create_segment(
        client,
        settings,
        relative_path="cam-001/b.mp4",
        start=base + timedelta(seconds=60),
        end=base + timedelta(seconds=120),
    )
    response = client.post(
        f"{BASE}/events",
        headers=HEADERS,
        json={
            "camera_id": "cam-001",
            "event_type": "person_detected",
            "occurred_at": (base + timedelta(seconds=60)).isoformat(),
            "person_id": "track-7",
            "global_person_id": "global-2",
            "confidence": 0.91,
            "recording_segment_ids": [first["id"]],
            "metadata": {"label": "사람"},
        },
    )
    assert response.status_code == 201, response.text
    event = response.json()
    assert event["person_id"] == "track-7"
    assert event["global_person_id"] == "global-2"
    assert "track_id" not in event
    assert event["recording_segment_ids"] == [first["id"], second["id"]]
    assert event["recording_segment_id"] == first["id"]
    assert event["metadata"] == {"label": "사람"}


# 이벤트를 먼저 저장해도 나중에 등록된 녹화가 전후 버퍼 구간으로 연결되어야 한다.
def test_later_segment_is_linked_to_event_pre_and_post_roll_window(data_client) -> None:
    client, settings = data_client
    _create_camera(client, "cam-001")
    occurred = datetime(2026, 8, 22, 8, 1, tzinfo=UTC)
    created = client.post(
        f"{BASE}/events",
        headers=HEADERS,
        json={
            "camera_id": "cam-001",
            "event_type": "person_appeared",
            "occurred_at": occurred.isoformat(),
        },
    ).json()
    assert created["recording_segment_ids"] == []

    segment = _create_segment(
        client,
        settings,
        relative_path="cam-001/future-index.mp4",
        start=occurred + timedelta(seconds=5),
        end=occurred + timedelta(seconds=15),
    )
    stored = client.get(f"{BASE}/events/{created['id']}", headers=HEADERS).json()
    assert stored["recording_segment_ids"] == [segment["id"]]
    assert stored["recording_segment_id"] == segment["id"]


# 일반 사용자는 배정 카메라만 보고 관리자는 전체 카메라를 볼 수 있어야 한다.
def test_camera_acl_filters_viewer_but_not_admin(data_client) -> None:
    client, _settings = data_client
    viewer = _create_user(client, "viewer")
    admin = _create_user(client, "admin", role="admin")
    _create_camera(client, "cam-001")
    _create_camera(client, "cam-002")
    grant = client.put(
        f"{BASE}/users/{viewer['id']}/camera-permissions/cam-002",
        headers=HEADERS,
    )
    assert grant.status_code == 200

    viewer_result = client.get(
        f"{BASE}/cameras", headers=HEADERS, params={"user_id": viewer["id"]}
    ).json()
    admin_result = client.get(
        f"{BASE}/cameras", headers=HEADERS, params={"user_id": admin["id"]}
    ).json()
    assert [item["camera_id"] for item in viewer_result["items"]] == ["cam-002"]
    assert {item["camera_id"] for item in admin_result["items"]} == {
        "cam-001",
        "cam-002",
    }


# 송출 해시를 내부에서 조회할 수 있고 카메라 삭제와 함께 자격 증명도 제거되어야 한다.
def test_camera_publish_credential_is_internal_and_cascades(data_client) -> None:
    client, _settings = data_client
    _create_camera(client, "cam-001")
    stored = client.put(
        f"{BASE}/cameras/cam-001/publish-credential",
        headers=HEADERS,
        json={"username": "cam-001", "password_hash": "$argon2id$dynamic-test"},
    )
    assert stored.status_code == 200
    assert stored.json()["password_hash"].startswith("$argon2")
    assert (
        client.get(
            f"{BASE}/cameras/cam-001/publish-credential", headers=HEADERS
        ).status_code
        == 200
    )
    assert client.delete(f"{BASE}/cameras/cam-001", headers=HEADERS).status_code == 204
    assert (
        client.get(
            f"{BASE}/cameras/cam-001/publish-credential", headers=HEADERS
        ).status_code
        == 404
    )


# 운영체제별 절대 경로와 상위 경로 우회 모두 저장 루트 검증에서 거절되어야 한다.
@pytest.mark.parametrize(
    "bad_path", ["../escape.mp4", "/absolute.mp4", "C:\\escape.mp4"]
)
def test_path_traversal_and_absolute_paths_are_rejected(
    data_client, bad_path: str
) -> None:
    client, _settings = data_client
    _create_camera(client, "cam-001")
    start = datetime(2026, 8, 22, tzinfo=UTC)
    response = client.post(
        f"{BASE}/recording-segments",
        headers=HEADERS,
        json={
            "camera_id": "cam-001",
            "start_time": start.isoformat(),
            "end_time": (start + timedelta(seconds=60)).isoformat(),
            "relative_path": bad_path,
            "format": "mp4",
            "source": "central",
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "INVALID_STORAGE_PATH"


# DB에는 있지만 파일이 없는 항목과 파일만 존재하는 항목을 서로 다르게 보고한다.
def test_reconcile_marks_missing_and_reports_orphan(data_client) -> None:
    client, settings = data_client
    _create_camera(client, "cam-001")
    start = datetime(2026, 8, 22, tzinfo=UTC)
    segment = _create_segment(
        client,
        settings,
        relative_path="cam-001/missing.mp4",
        start=start,
        end=start + timedelta(seconds=60),
    )
    (settings.storage_root / segment["relative_path"]).unlink()
    orphan = settings.storage_root / "cam-001/orphan.mp4"
    orphan.write_bytes(b"orphan")

    response = client.post(f"{BASE}/reconcile", headers=HEADERS)
    assert response.status_code == 200, response.text
    assert response.json()["missing"] == ["cam-001/missing.mp4"]
    assert response.json()["orphaned"] == ["cam-001/orphan.mp4"]
    stored = client.get(
        f"{BASE}/recording-segments/{segment['id']}", headers=HEADERS
    ).json()
    assert stored["status"] == "missing"


# 완료 훅을 놓친 정상 중앙 녹화 파일을 대조 작업이 다시 등록할 수 있어야 한다.
def test_reconcile_indexes_completed_mediamtx_segment_after_hook_failure(
    data_client,
) -> None:
    client, settings = data_client
    _create_camera(client, "cam-001")
    start = datetime(2026, 8, 22, 12, 34, 56, 123456, tzinfo=UTC)
    end = start + timedelta(seconds=60)
    relative_path = "cam-001/2026/08/22/20260822T123456-123456Z.mp4"
    target = settings.storage_root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"completed-central-recording")
    timestamp = end.timestamp()
    os.utime(target, (timestamp, timestamp))

    response = client.post(f"{BASE}/reconcile", headers=HEADERS)

    assert response.status_code == 200, response.text
    assert response.json()["indexed_orphans"] == [relative_path]
    assert response.json()["orphaned"] == []
    indexed = client.get(
        f"{BASE}/recording-segments/search",
        headers=HEADERS,
        params={
            "camera_id": "cam-001",
            "from": start.isoformat(),
            "to": (end + timedelta(seconds=1)).isoformat(),
        },
    )
    assert indexed.status_code == 200, indexed.text
    assert indexed.json()["items"][0]["relative_path"] == relative_path
    assert indexed.json()["items"][0]["source"] == "central"

    replay = client.post(f"{BASE}/reconcile", headers=HEADERS)
    assert replay.status_code == 200
    assert replay.json()["indexed_orphans"] == []


# 삭제 후보 조회·실제 파일 삭제·DB 백업이 각각 의도한 결과를 남기는지 확인한다.
def test_backup_and_retention_cleanup(data_client) -> None:
    client, settings = data_client
    _create_camera(client, "cam-001")
    start = datetime(2025, 1, 1, tzinfo=UTC)
    segment = _create_segment(
        client,
        settings,
        relative_path="cam-001/old.mp4",
        start=start,
        end=start + timedelta(seconds=60),
    )
    dry_run = client.post(
        f"{BASE}/retention/cleanup",
        headers=HEADERS,
        json={"before": "2026-01-01T00:00:00Z", "dry_run": True},
    )
    assert dry_run.status_code == 200
    assert dry_run.json()["segment_ids"] == [segment["id"]]
    cleanup = client.post(
        f"{BASE}/retention/cleanup",
        headers=HEADERS,
        json={"before": "2026-01-01T00:00:00Z", "dry_run": False},
    )
    assert cleanup.json()["deleted"] == 1
    assert not (settings.storage_root / "cam-001/old.mp4").exists()

    backup = client.post(
        f"{BASE}/backup", headers=HEADERS, json={"filename": "manual.db"}
    )
    assert backup.status_code == 201, backup.text
    backup_path = settings.backup_root / backup.json()["relative_path"]
    assert backup_path.is_file()
    with sqlite3.connect(backup_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


# deleting 상태로 중단된 파일은 다음 대조 작업에서 삭제 완료 상태로 수렴한다.
@pytest.mark.parametrize("file_exists", [True, False])
def test_reconcile_completes_interrupted_retention_delete(
    data_client, file_exists: bool
) -> None:
    client, settings = data_client
    _create_camera(client, "cam-001")
    start = datetime(2025, 1, 1, tzinfo=UTC)
    segment = _create_segment(
        client,
        settings,
        relative_path="cam-001/interrupted.mp4",
        start=start,
        end=start + timedelta(seconds=60),
    )
    target = settings.storage_root / segment["relative_path"]
    if not file_exists:
        target.unlink()
    repository = client.app.state.repository
    repository.set_segment_status(segment["id"], "deleting")

    response = client.post(f"{BASE}/reconcile", headers=HEADERS)

    assert response.status_code == 200
    assert response.json()["completed_deletions"] == ["cam-001/interrupted.mp4"]
    assert not target.exists()
    stored = client.get(
        f"{BASE}/recording-segments/{segment['id']}", headers=HEADERS
    ).json()
    assert stored["status"] == "deleted"


# 새 갱신 토큰 발급 뒤 이전 토큰의 폐기·교체 이력이 남고 접근 폐기도 조회되어야 한다.
def test_refresh_rotation_and_revoked_token_state(data_client) -> None:
    client, _settings = data_client
    user = _create_user(client, "token-user")
    expires = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    old = client.post(
        f"{BASE}/tokens/refresh",
        headers=HEADERS,
        json={
            "user_id": user["id"],
            "jti": "old-jti",
            "token_hash": "old-hash",
            "expires_at": expires,
            "family_id": "family-1",
        },
    )
    assert old.status_code == 201, old.text
    new = client.post(
        f"{BASE}/tokens/refresh",
        headers=HEADERS,
        json={
            "user_id": user["id"],
            "jti": "new-jti",
            "token_hash": "new-hash",
            "expires_at": expires,
            "family_id": "family-1",
            "rotated_from_jti": "old-jti",
        },
    )
    assert new.status_code == 201, new.text
    old_state = client.get(f"{BASE}/tokens/refresh/old-jti", headers=HEADERS).json()
    assert old_state["revoked_at"] is not None
    assert old_state["replaced_by_jti"] == "new-jti"

    put = client.put(
        f"{BASE}/tokens/revoked/access-jti",
        headers=HEADERS,
        json={
            "user_id": user["id"],
            "expires_at": expires,
            "reason": "logout",
        },
    )
    assert put.status_code == 200
    assert (
        client.get(f"{BASE}/tokens/revoked/access-jti", headers=HEADERS).json()[
            "reason"
        ]
        == "logout"
    )
    assert (
        client.delete(f"{BASE}/tokens/refresh/new-jti", headers=HEADERS).status_code
        == 204
    )
    revoked = client.get(f"{BASE}/tokens/refresh/new-jti", headers=HEADERS)
    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None


# 시간대 없는 시각은 서버 로컬 시간으로 추정하지 않고 검증 오류로 거절한다.
def test_naive_timestamps_are_rejected(data_client) -> None:
    client, _settings = data_client
    _create_camera(client, "cam-001")
    response = client.post(
        f"{BASE}/events",
        headers=HEADERS,
        json={
            "camera_id": "cam-001",
            "event_type": "person_detected",
            "occurred_at": "2026-08-22T10:00:00",
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


# 초기화가 반복되어도 첫 관리자와 설정 파일의 카메라가 중복 생성되지 않아야 한다.
def test_first_start_bootstraps_admin_and_cameras_idempotently(tmp_path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """schema_version: 1
cameras:
  - camera_id: cam-001
    name: Entrance
""",
        encoding="utf-8",
    )
    settings = Settings(
        database_path=tmp_path / "database" / "ai_cctv.db",
        storage_root=tmp_path / "recordings",
        snapshot_root=tmp_path / "snapshots",
        backup_root=tmp_path / "backups",
        internal_token=TOKEN,
        initial_admin_username="admin",
        initial_admin_password_hash="$argon2id$bootstrap-test",
        config_path=config_path,
    )
    for _ in range(2):
        with TestClient(create_app(settings=settings)) as client:
            users = client.get(f"{BASE}/users", headers=HEADERS).json()["items"]
            cameras = client.get(f"{BASE}/cameras", headers=HEADERS).json()["items"]
            assert [user["username"] for user in users] == ["admin"]
            assert [camera["camera_id"] for camera in cameras] == ["cam-001"]


# 장치 인증값과 공개 카메라 정보 및 요청·현재 프로필·실행 관측값의 경계를 검증한다.
def test_edge_metadata_profiles_and_runtime_state_are_separate(data_client) -> None:
    client, _settings = data_client
    token = "e" * 32
    created = client.post(
        f"{BASE}/cameras",
        headers=HEADERS,
        json={
            "camera_id": "cam-001",
            "name": "Entrance",
            "stream_path": "cam-001",
            "edge_device_id": "edge-001",
            "edge_management_url": "http://edge.test:8003",
            "edge_recovery_url": "http://edge.test:8002",
            "edge_auth_token": token,
        },
    )
    assert created.status_code == 201, created.text
    assert "edge_auth_token" not in created.json()

    target = client.get(
        f"{BASE}/cameras/cam-001/control-target", headers=HEADERS
    ).json()
    assert target["management_url"] == "http://edge.test:8003"
    assert target["auth_token"] == token

    profile = client.get(
        f"{BASE}/cameras/cam-001/video-profile", headers=HEADERS
    ).json()
    assert profile["current_profile"] == "hd"
    assert profile["desired_profile"] == "hd"
    changed = client.patch(
        f"{BASE}/cameras/cam-001/video-profile",
        headers=HEADERS,
        json={"desired_profile": "fhd"},
    ).json()
    assert changed["desired_profile"] == "fhd"
    assert changed["current_profile"] == "hd"

    first_status = client.put(
        f"{BASE}/cameras/cam-001/runtime-status",
        headers=HEADERS,
        json={
            "online": True,
            "cpu_percent": 12.5,
            "memory_percent": 34.5,
            "storage_percent": 56.5,
            "battery_percent": 78,
            "power_source": "battery",
            "camera_input": "online",
            "central_connection_status": "online",
            "current_video_profile": "hd",
            "last_seen_at": "2026-08-23T07:20:00Z",
        },
    )
    assert first_status.status_code == 200, first_status.text
    offline = client.put(
        f"{BASE}/cameras/cam-001/runtime-status",
        headers=HEADERS,
        json={"online": False, "last_error_code": "EDGE_OFFLINE"},
    )
    assert offline.status_code == 200, offline.text
    status_payload = client.get(
        f"{BASE}/cameras/cam-001/runtime-status", headers=HEADERS
    ).json()
    assert status_payload["online"] is False
    assert status_payload["cpu_percent"] == 12.5
    assert status_payload["camera_input"] == "online"
    assert status_payload["current_video_profile"] == "hd"

    rotated = client.patch(
        f"{BASE}/cameras/cam-001",
        headers=HEADERS,
        json={"edge_auth_token": "r" * 32},
    )
    assert rotated.status_code == 200, rotated.text
    rotated_target = client.get(
        f"{BASE}/cameras/cam-001/control-target", headers=HEADERS
    ).json()
    assert rotated_target["auth_token"] == "r" * 32
    assert rotated_target["management_url"] == target["management_url"]

    incomplete = client.patch(
        f"{BASE}/cameras/cam-001",
        headers=HEADERS,
        json={"edge_device_id": "edge-new"},
    )
    assert incomplete.status_code == 422

    assert client.delete(f"{BASE}/cameras/cam-001", headers=HEADERS).status_code == 204
    assert (
        client.get(f"{BASE}/edge-devices/edge-001", headers=HEADERS).status_code == 404
    )


# 인증값이나 해석이 모호한 경로를 포함하는 Edge URL을 등록할 수 없어야 한다.
def test_edge_service_urls_reject_ambiguous_or_credentialed_paths(data_client) -> None:
    client, _settings = data_client
    common = {
        "camera_id": "cam-001",
        "name": "Entrance",
        "stream_path": "cam-001",
        "edge_device_id": "edge-001",
        "edge_recovery_url": "http://edge.test:8002",
        "edge_auth_token": "e" * 32,
    }
    for invalid in (
        "http://user:pass@edge.test:8003",
        "http://edge.test:8003/control?token=secret",
        "http://edge.test:8003/a/../control",
    ):
        response = client.post(
            f"{BASE}/cameras",
            headers=HEADERS,
            json={**common, "edge_management_url": invalid},
        )
        assert response.status_code == 422


# 중복 Edge 이벤트와 복구 작업의 상태 전이·재시작 후 재대기 처리를 함께 확인한다.
def test_edge_event_idempotency_recovery_lifecycle_and_crash_requeue(
    data_client,
) -> None:
    client, _settings = data_client
    response = client.post(
        f"{BASE}/cameras",
        headers=HEADERS,
        json={
            "camera_id": "cam-001",
            "name": "Entrance",
            "stream_path": "cam-001",
            "edge_device_id": "edge-001",
            "edge_management_url": "http://edge.test:8003",
            "edge_recovery_url": "http://edge.test:8002",
            "edge_auth_token": "e" * 32,
        },
    )
    assert response.status_code == 201, response.text
    base = datetime(2026, 8, 22, 7, 30, tzinfo=UTC)
    lost_payload = {
        "camera_id": "cam-001",
        "event_type": "central_connection_lost",
        "occurred_at": base.isoformat(),
        "edge_event_id": "edge-001:lost-1",
    }
    first = client.post(f"{BASE}/events", headers=HEADERS, json=lost_payload)
    replay = client.post(f"{BASE}/events", headers=HEADERS, json=lost_payload)
    assert first.status_code == replay.status_code == 201
    assert first.json()["id"] == replay.json()["id"]
    jobs = client.get(f"{BASE}/recovery-jobs", headers=HEADERS).json()["items"]
    assert len(jobs) == 1
    assert jobs[0]["status"] == "detected"

    restored = client.post(
        f"{BASE}/events",
        headers=HEADERS,
        json={
            "camera_id": "cam-001",
            "event_type": "central_connection_restored",
            "occurred_at": (base + timedelta(minutes=2)).isoformat(),
            "edge_event_id": "edge-001:restored-1",
        },
    )
    assert restored.status_code == 201, restored.text
    job = client.get(f"{BASE}/recovery-jobs", headers=HEADERS).json()["items"][0]
    assert job["status"] == "waiting_for_recovery"

    # A delayed duplicate reporter inside the same interval must not enqueue a
    # second transfer job.
    delayed = dict(lost_payload)
    delayed["edge_event_id"] = "inference:lost-duplicate"
    delayed["occurred_at"] = (base + timedelta(seconds=1)).isoformat()
    assert (
        client.post(f"{BASE}/events", headers=HEADERS, json=delayed).status_code == 201
    )
    assert (
        len(client.get(f"{BASE}/recovery-jobs", headers=HEADERS).json()["items"]) == 1
    )

    repository = client.app.state.repository
    claimed = repository.claim_due_recovery_job()
    assert claimed is not None
    assert claimed["status"] == "downloading"
    assert claimed["attempt_count"] == 1
    assert repository.requeue_interrupted_recovery_jobs() == 1
    requeued = repository.get_recovery_job(int(claimed["id"]))
    assert requeued is not None
    assert requeued["status"] == "failed"
    assert requeued["last_error"] == "RECOVERY_INTERRUPTED"
    assert requeued["next_retry_at"] is not None


# 순서가 뒤바뀐 보고는 복구 구간을 확장하고 이전 revision의 완료 보고를 무효화해야 한다.
def test_recovery_merges_out_of_order_reporter_boundaries(data_client) -> None:
    client, _settings = data_client
    _create_camera(client, "cam-001")
    base = datetime(2026, 8, 22, 7, 30, tzinfo=UTC)

    def event(event_id: str, event_type: str, occurred_at: datetime) -> None:
        response = client.post(
            f"{BASE}/events",
            headers=HEADERS,
            json={
                "camera_id": "cam-001",
                "event_type": event_type,
                "occurred_at": occurred_at.isoformat(),
                "edge_event_id": event_id,
            },
        )
        assert response.status_code == 201, response.text

    # Transport ordering is intentionally reversed: restore is persisted first.
    event(
        "inference:restored-early",
        "central_connection_restored",
        base + timedelta(minutes=2),
    )
    event(
        "edge:lost-late",
        "central_connection_lost",
        base + timedelta(seconds=10),
    )
    event("edge:lost-earlier", "central_connection_lost", base)
    repository = client.app.state.repository
    claimed = repository.claim_due_recovery_job()
    assert claimed is not None
    stale_revision = int(claimed["revision"])
    event(
        "edge:restored-later",
        "central_connection_restored",
        base + timedelta(minutes=2, seconds=20),
    )

    jobs = client.get(f"{BASE}/recovery-jobs", headers=HEADERS).json()["items"]
    assert len(jobs) == 1
    job = jobs[0]
    assert job["status"] == "waiting_for_recovery"
    assert (
        datetime.fromisoformat(job["outage_started_at"].replace("Z", "+00:00")) == base
    )
    assert datetime.fromisoformat(
        job["outage_ended_at"].replace("Z", "+00:00")
    ) == base + timedelta(minutes=2, seconds=20)
    assert job["revision"] == 2
    assert (
        repository.update_recovery_job(
            int(job["id"]),
            status="completed",
            expected_revision=stale_revision,
        )
        is None
    )
    assert repository.get_recovery_job(int(job["id"]))["status"] == (
        "waiting_for_recovery"
    )

    # A later distinct outage starts a new detected interval.
    event(
        "edge:lost-new",
        "central_connection_lost",
        base + timedelta(minutes=5),
    )
    jobs = client.get(f"{BASE}/recovery-jobs", headers=HEADERS).json()["items"]
    assert len(jobs) == 2
    assert {item["status"] for item in jobs} == {
        "waiting_for_recovery",
        "detected",
    }
    event(
        "edge:lost-old-delayed",
        "central_connection_lost",
        base + timedelta(minutes=1),
    )
    jobs = client.get(f"{BASE}/recovery-jobs", headers=HEADERS).json()["items"]
    assert len(jobs) == 2
    detected = next(item for item in jobs if item["status"] == "detected")
    assert datetime.fromisoformat(
        detected["outage_started_at"].replace("Z", "+00:00")
    ) == base + timedelta(minutes=5)


# 복구 보고 이후 안정화 시간이 지나야 작업을 할당하고 종료 경계가 늘면 대기도 갱신한다.
def test_recovery_waits_for_final_edge_segment_to_settle(data_client) -> None:
    client, _settings = data_client
    _create_camera(client, "cam-001")
    base = datetime.now(UTC).replace(microsecond=0) + timedelta(minutes=1)

    for event_id, event_type, occurred_at in (
        ("edge:lost", "central_connection_lost", base),
        (
            "edge:restored",
            "central_connection_restored",
            base + timedelta(seconds=20),
        ),
    ):
        response = client.post(
            f"{BASE}/events",
            headers=HEADERS,
            json={
                "camera_id": "cam-001",
                "event_type": event_type,
                "occurred_at": occurred_at.isoformat(),
                "edge_event_id": event_id,
            },
        )
        assert response.status_code == 201, response.text

    repository = client.app.state.repository
    job = repository.list_recovery_jobs("cam-001", 10, 0)[0]
    assert datetime.fromisoformat(
        job["next_retry_at"].replace("Z", "+00:00")
    ) == base + timedelta(seconds=35)
    assert repository.claim_due_recovery_job() is None

    later_restore = client.post(
        f"{BASE}/events",
        headers=HEADERS,
        json={
            "camera_id": "cam-001",
            "event_type": "central_connection_restored",
            "occurred_at": (base + timedelta(seconds=30)).isoformat(),
            "edge_event_id": "inference:restored-later",
        },
    )
    assert later_restore.status_code == 201
    job = repository.list_recovery_jobs("cam-001", 10, 0)[0]
    assert datetime.fromisoformat(
        job["next_retry_at"].replace("Z", "+00:00")
    ) == base + timedelta(seconds=45)
    assert repository.claim_due_recovery_job() is None


# 구형 추론 연결 이벤트는 이력만 남기고 Edge 중앙 송출의 복구 구간을 만들거나 닫지 않는다.
def test_legacy_network_events_are_stored_without_recovery_side_effects(
    data_client,
) -> None:
    client, _settings = data_client
    _create_camera(client, "cam-001")
    base = datetime(2026, 8, 22, 7, 30, tzinfo=UTC)

    def event(event_id: str, event_type: str, occurred_at: datetime) -> None:
        response = client.post(
            f"{BASE}/events",
            headers=HEADERS,
            json={
                "camera_id": "cam-001",
                "event_type": event_type,
                "occurred_at": occurred_at.isoformat(),
                "edge_event_id": event_id,
            },
        )
        assert response.status_code == 201, response.text

    # Legacy inference-consumer aliases remain searchable event history, but
    # cannot create an Edge segment-recovery interval by themselves.
    event("legacy:lost-only", "network_failure", base)
    event("legacy:restored-only", "network_recovery", base + timedelta(minutes=5))
    repository = client.app.state.repository
    assert repository.list_recovery_jobs("cam-001", 10, 0) == []

    event("edge:lost", "central_connection_lost", base + timedelta(minutes=1))
    detected = repository.list_recovery_jobs("cam-001", 10, 0)
    assert len(detected) == 1
    assert detected[0]["status"] == "detected"
    assert detected[0]["outage_ended_at"] is None
    event(
        "edge:restored",
        "central_connection_restored",
        base + timedelta(minutes=2),
    )
    before = repository.list_recovery_jobs("cam-001", 10, 0)
    assert len(before) == 1

    # Nor may delayed legacy aliases expand an authoritative Edge interval.
    event("legacy:lost-earlier", "network_failure", base - timedelta(minutes=1))
    event(
        "legacy:restored-later",
        "network_recovery",
        base + timedelta(minutes=3),
    )
    after = repository.list_recovery_jobs("cam-001", 10, 0)
    assert len(after) == 1
    assert after[0]["outage_started_at"] == before[0]["outage_started_at"]
    assert after[0]["outage_ended_at"] == before[0]["outage_ended_at"]
    assert after[0]["revision"] == before[0]["revision"]

    stored = repository.search_events(
        camera_id="cam-001",
        event_type=None,
        start_time=None,
        end_time=None,
        limit=20,
        offset=0,
    )
    assert {item["edge_event_id"] for item in stored} >= {
        "legacy:lost-only",
        "legacy:restored-only",
        "legacy:lost-earlier",
        "legacy:restored-later",
    }


# 복구 영상의 일부 바이트 요청에 206·Content-Range·MPEG-TS 형식이 올바르게 반환되어야 한다.
def test_recording_content_supports_mpegts_range_requests(data_client) -> None:
    client, settings = data_client
    _create_camera(client, "cam-001")
    relative_path = "cam-001/recovered/segment.ts"
    content = b"\x47recovered-mpegts-content"
    target = settings.storage_root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    start = datetime(2026, 8, 23, 7, 30, tzinfo=UTC)
    created = client.post(
        f"{BASE}/recording-segments",
        headers=HEADERS,
        json={
            "camera_id": "cam-001",
            "start_time": start.isoformat(),
            "end_time": (start + timedelta(seconds=10)).isoformat(),
            "relative_path": relative_path,
            "format": "mpegts",
            "source": "edge_recovery",
        },
    )
    assert created.status_code == 201, created.text
    segment_id = created.json()["id"]

    response = client.get(
        f"{BASE}/recording-segments/{segment_id}/content",
        headers={**HEADERS, "Range": "bytes=1-5"},
    )
    assert response.status_code == 206
    assert response.content == content[1:6]
    assert response.headers["content-range"] == f"bytes 1-5/{len(content)}"
    assert response.headers["content-type"].startswith("video/mp2t")

    etag = client.get(
        f"{BASE}/recording-segments/{segment_id}/content",
        headers=HEADERS,
    ).headers["etag"]
    stale_validator = client.get(
        f"{BASE}/recording-segments/{segment_id}/content",
        headers={**HEADERS, "Range": "bytes=1-5", "If-Range": '"stale"'},
    )
    matching_validator = client.get(
        f"{BASE}/recording-segments/{segment_id}/content",
        headers={**HEADERS, "Range": "bytes=1-5", "If-Range": etag},
    )
    assert stale_validator.status_code == 200
    assert stale_validator.content == content
    assert matching_validator.status_code == 206
    assert matching_validator.content == content[1:6]


# 활성 수·이력 보존·권한 교체 제약의 실패가 기존 DB 상태를 부분 변경하지 않아야 한다.
def test_camera_limit_history_delete_and_permission_replace_are_transactional(
    data_client,
) -> None:
    client, settings = data_client
    user = _create_user(client, "operator")
    for index in range(1, 5):
        _create_camera(client, f"cam-00{index}")
    limited = client.post(
        f"{BASE}/cameras",
        headers=HEADERS,
        json={
            "camera_id": "cam-005",
            "name": "cam-005",
            "stream_path": "cam-005",
        },
    )
    assert limited.status_code == 409
    assert limited.json()["error"]["code"] == "CAMERA_LIMIT_REACHED"

    disabled = client.patch(
        f"{BASE}/cameras/cam-001",
        headers=HEADERS,
        json={"enabled": False, "status": "disabled"},
    )
    assert disabled.status_code == 200
    replacement = client.post(
        f"{BASE}/cameras",
        headers=HEADERS,
        json={
            "camera_id": "cam-005",
            "name": "cam-005",
            "stream_path": "cam-005",
        },
    )
    assert replacement.status_code == 201
    over_limit_enable = client.patch(
        f"{BASE}/cameras/cam-001",
        headers=HEADERS,
        json={"enabled": True, "status": "offline"},
    )
    assert over_limit_enable.status_code == 409
    assert over_limit_enable.json()["error"]["code"] == "CAMERA_LIMIT_REACHED"

    # Restore this fixture's original four-camera set for history tests below.
    assert client.delete(f"{BASE}/cameras/cam-005", headers=HEADERS).status_code == 204
    assert (
        client.patch(
            f"{BASE}/cameras/cam-001",
            headers=HEADERS,
            json={"enabled": True, "status": "offline"},
        ).status_code
        == 200
    )

    replaced = client.put(
        f"{BASE}/users/{user['id']}/camera-permissions",
        headers=HEADERS,
        json={"camera_ids": ["cam-001"]},
    )
    assert replaced.status_code == 200
    assert [item["camera_id"] for item in replaced.json()["items"]] == ["cam-001"]
    failed_replace = client.put(
        f"{BASE}/users/{user['id']}/camera-permissions",
        headers=HEADERS,
        json={"camera_ids": ["cam-002", "cam-missing"]},
    )
    assert failed_replace.status_code == 404
    unchanged = client.get(
        f"{BASE}/users/{user['id']}/camera-permissions", headers=HEADERS
    )
    assert [item["camera_id"] for item in unchanged.json()["items"]] == ["cam-001"]

    start = datetime(2026, 8, 23, 8, 0, tzinfo=UTC)
    _create_segment(
        client,
        settings,
        camera_id="cam-001",
        relative_path="cam-001/history.mp4",
        start=start,
        end=start + timedelta(seconds=10),
    )
    deletion_status = client.get(
        f"{BASE}/cameras/cam-001/deletion-status", headers=HEADERS
    )
    assert deletion_status.status_code == 200
    assert deletion_status.json() == {
        "camera_id": "cam-001",
        "deletable": False,
        "reason_code": "CAMERA_HAS_HISTORY",
    }
    conflict = client.delete(f"{BASE}/cameras/cam-001", headers=HEADERS)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "CAMERA_HAS_HISTORY"
    assert client.get(f"{BASE}/cameras/cam-001", headers=HEADERS).status_code == 200
