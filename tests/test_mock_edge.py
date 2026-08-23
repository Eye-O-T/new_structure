from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from configurator.edge_discovery import parse_advertisement
from mock_edge.app import MockEdgeService, create_control_app, create_recovery_app
from mock_edge.protocol import build_advertisement
from mock_edge.runtime import (
    CentralTarget,
    VIDEO_PROFILES,
    build_publisher_command,
    build_recorder_command,
)


PAIRING_KEY = "mock-edge-shared-key-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {PAIRING_KEY}"}


class FakeMediaEngine:
    def __init__(self) -> None:
        self.configured = False
        self.target: CentralTarget | None = None
        self.profile = "hd"
        self.publisher_running = False
        self.recorder_running = False
        self.publisher_suspended = False
        self.last_error: str | None = None
        self.started = False

    def configure(self, target: CentralTarget, profile: str) -> None:
        self.target = target
        self.profile = profile
        self.configured = True
        if self.started:
            self.publisher_running = True
            self.recorder_running = True

    def start(self) -> None:
        self.started = True
        if self.configured:
            self.publisher_running = True
            self.recorder_running = True

    def stop(self) -> None:
        self.started = False
        self.publisher_running = False
        self.recorder_running = False

    def apply_profile(self, profile: str) -> tuple[bool, str | None]:
        self.profile = profile
        return True, None

    def suspend_publisher(self) -> None:
        self.publisher_suspended = True
        self.publisher_running = False

    def resume_publisher(self) -> None:
        self.publisher_suspended = False
        self.publisher_running = self.started and self.configured


def make_service(tmp_path: Path) -> tuple[MockEdgeService, FakeMediaEngine]:
    media = FakeMediaEngine()
    video = tmp_path / "input.mp4"
    video.write_bytes(b"not-used-by-fake-media")
    service = MockEdgeService(
        device_id="mock-edge-001",
        camera_id="cam-001",
        pairing_key=PAIRING_KEY,
        state_root=tmp_path / "state",
        backup_root=tmp_path / "recordings",
        video_path=video,
        media=media,  # type: ignore[arg-type]
    )
    return service, media


def pairing_body() -> dict[str, object]:
    return {
        "device_id": "mock-edge-001",
        "camera_id": "cam-001",
        "central_host": "127.0.0.1",
        "central_port": 8554,
        "backup_root": "/var/lib/ai-cctv-edge/recordings",
        "video_profile": "hd",
        "supported_profiles": ["hd", "fhd"],
        "publish_username": "cam-001",
        "publish_password": "publish-secret-0123456789",
    }


def test_discovery_packet_is_accepted_by_configurator() -> None:
    payload = build_advertisement(
        device_id="mock-edge-001",
        camera_id="cam-001",
        management_port=8003,
        recovery_port=8002,
        supported_profiles=("hd", "fhd"),
        pairing_key=PAIRING_KEY,
        sent_at=1_777_000_000,
        message_id="426414c6-a171-4e2f-b282-5bfb4eb66b61",
    )

    edge = parse_advertisement(
        payload, "127.0.0.1", PAIRING_KEY, now=1_777_000_000
    )

    assert edge.device_id == "mock-edge-001"
    assert edge.camera_id == "cam-001"
    assert edge.management_url == "http://127.0.0.1:8003"
    assert edge.recovery_url == "http://127.0.0.1:8002"
    assert edge.supported_profiles == ("hd", "fhd")


def test_loop_sender_style_commands_publish_and_record(tmp_path: Path) -> None:
    video = tmp_path / "source file.mp4"
    target = CentralTarget(
        host="127.0.0.1",
        port=8554,
        camera_id="cam-001",
        username="cam-001",
        password="secret-with-:@-012345",
    )

    publish = build_publisher_command("ffmpeg", video, VIDEO_PROFILES["hd"], target)
    record, pattern = build_recorder_command(
        "ffmpeg",
        video,
        VIDEO_PROFILES["hd"],
        tmp_path / "backup",
        "cam-001",
        10,
        now=datetime(2026, 8, 24, 1, 2, 3, tzinfo=UTC),
    )

    assert publish[:1] == ["ffmpeg"]
    assert "-stream_loop" in publish and publish[publish.index("-stream_loop") + 1] == "-1"
    assert "-re" in publish
    assert "libx264" in publish
    assert publish[-2:] == ["rtsp", target.rtsp_url]
    assert "secret-with-:@-012345" not in target.rtsp_url
    assert "-segment_time" in record
    assert pattern.name == "20260824T010203.000000Z_%06d.ts"
    assert pattern.parent.as_posix().endswith("cam-001/2026/08/24")


def test_pairing_management_profile_and_event_contract(tmp_path: Path) -> None:
    service, media = make_service(tmp_path)
    client = TestClient(create_control_app(service))

    assert client.get("/health/live").json()["status"] == "pairing"
    assert client.get("/internal/v1/status").status_code == 401
    response = client.put(
        "/internal/v1/pairing/complete", headers=AUTH, json=pairing_body()
    )

    assert response.status_code == 200
    assert response.json() == {
        "status": "configured",
        "device_id": "mock-edge-001",
        "camera_id": "cam-001",
        "rtsp_mode": "central_publish",
    }
    assert media.target is not None
    assert media.target.rtsp_url.startswith("rtsp://cam-001:")
    persisted = (tmp_path / "state" / "mock-edge.json").read_text(encoding="utf-8")
    assert "publish-secret-0123456789" not in persisted
    assert (tmp_path / "state" / "publish.password").read_text(
        encoding="utf-8"
    ).strip() == "publish-secret-0123456789"
    assert client.get("/health/live").json()["status"] == "alive"
    assert (
        client.put(
            "/internal/v1/pairing/complete", headers=AUTH, json=pairing_body()
        ).status_code
        == 409
    )

    service.start()
    status = client.get("/internal/v1/status", headers=AUTH).json()
    assert status["camera_input_status"] == "online"
    assert status["central_connection_status"] == "online"
    assert status["current_video_profile"] == "hd"
    assert status["power_source"] == "external"
    capabilities = client.get(
        "/internal/v1/capabilities/video", headers=AUTH
    ).json()
    assert capabilities["supported_profiles"] == ["hd", "fhd"]
    assert capabilities["codec"] == "H.264"

    changed = client.put(
        "/internal/v1/config/video-profile",
        headers=AUTH,
        json={"profile": "fhd"},
    )
    assert changed.status_code == 200
    assert changed.json() == {
        "status": "applied",
        "previous_profile": "hd",
        "current_profile": "fhd",
    }
    first_page = client.get(
        "/internal/v1/events", headers=AUTH, params={"limit": 1}
    ).json()
    assert len(first_page["items"]) == 1
    second_page = client.get(
        "/internal/v1/events",
        headers=AUTH,
        params={"after": first_page["next_cursor"], "limit": 100},
    ).json()
    assert second_page["cursor_expired"] is False
    assert any(
        item["event_type"] == "video_profile_changed"
        for item in second_page["items"]
    )

    simulated = client.post(
        "/mock/v1/simulate",
        headers=AUTH,
        json={"action": "camera_input_lost"},
    )
    assert simulated.status_code == 200
    assert simulated.json()["edge"]["camera_input_status"] == "offline"
    power = client.post(
        "/mock/v1/simulate",
        headers=AUTH,
        json={"action": "external_power_lost", "battery_percent": 0},
    )
    assert power.status_code == 200
    assert power.json()["edge"]["power_source"] == "battery"
    assert power.json()["edge"]["battery_percent"] == 0
    storage = client.post(
        "/mock/v1/simulate",
        headers=AUTH,
        json={"action": "storage_critical", "storage_percent": 97},
    )
    assert storage.status_code == 200
    events = client.get("/internal/v1/events", headers=AUTH).json()["items"]
    assert any(item["event_type"] == "external_power_lost" for item in events)
    assert any(item["event_type"] == "storage_critical" for item in events)


def test_recovery_manifest_hash_download_and_open_segment_guard(tmp_path: Path) -> None:
    service, media = make_service(tmp_path)
    camera_root = tmp_path / "recordings" / "cam-001" / "2026" / "08" / "24"
    camera_root.mkdir(parents=True)
    closed = camera_root / "20260824T010000.000000Z_000000.ts"
    newest = camera_root / "20260824T010000.000000Z_000001.ts"
    closed.write_bytes(b"closed-segment")
    newest.write_bytes(b"still-open-segment")
    os.utime(closed, (1_777_000_000, 1_777_000_000))
    os.utime(newest, (1_777_000_001, 1_777_000_001))
    media.recorder_running = True
    client = TestClient(create_recovery_app(service))
    query = {
        "start": "2026-08-24T00:59:00Z",
        "end": "2026-08-24T01:02:00Z",
    }

    assert client.get("/v1/recovery/manifest", params=query).status_code == 401
    manifest = client.get(
        "/v1/recovery/manifest", headers=AUTH, params=query
    ).json()

    assert len(manifest["items"]) == 1
    item = manifest["items"][0]
    assert item["relative_path"].endswith(closed.name)
    assert item["size"] == len(b"closed-segment")
    assert item["sha256"] == hashlib.sha256(b"closed-segment").hexdigest()
    download = client.get(
        f"/v1/recovery/files/{item['relative_path']}", headers=AUTH
    )
    assert download.status_code == 200
    assert download.content == b"closed-segment"
    open_relative = newest.relative_to(tmp_path / "recordings" / "cam-001").as_posix()
    assert (
        client.get(f"/v1/recovery/files/{open_relative}", headers=AUTH).status_code
        == 409
    )

    media.recorder_running = False
    manifest = client.get(
        "/v1/recovery/manifest", headers=AUTH, params=query
    ).json()
    assert len(manifest["items"]) == 2


def test_stored_configuration_resumes_without_repairing(tmp_path: Path) -> None:
    service, _media = make_service(tmp_path)
    client = TestClient(create_control_app(service))
    assert (
        client.put(
            "/internal/v1/pairing/complete", headers=AUTH, json=pairing_body()
        ).status_code
        == 200
    )

    replacement = FakeMediaEngine()
    restored = MockEdgeService(
        device_id="mock-edge-001",
        camera_id="cam-001",
        pairing_key=PAIRING_KEY,
        state_root=tmp_path / "state",
        backup_root=tmp_path / "recordings",
        video_path=tmp_path / "input.mp4",
        media=replacement,  # type: ignore[arg-type]
    )

    assert restored.configured is True
    assert replacement.target is not None
    assert replacement.target.password == "publish-secret-0123456789"
    assert replacement.profile == "hd"
    assert json.loads(
        (tmp_path / "state" / "mock-edge.json").read_text(encoding="utf-8")
    )["local_backup_root"] == str((tmp_path / "recordings").resolve())
