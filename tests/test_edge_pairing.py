import json
from pathlib import Path

from fastapi.testclient import TestClient

from ai_cctv_edge.config import EdgeConfig
from ai_cctv_edge.pairing import (
    PairingSession,
    build_advertisement,
    create_pairing_app,
)
from configurator.edge_discovery import parse_advertisement
from configurator.edge_pairing import complete_edge_pairing


PAIRING_KEY = "p" * 48


def _advertisement(*, sent_at: int = 1_700_000_000) -> bytes:
    return build_advertisement(
        device_id="edge-001",
        camera_id="cam-001",
        management_port=8003,
        recovery_port=8002,
        supported_profiles=("hd", "fhd"),
        pairing_key=PAIRING_KEY,
        sent_at=sent_at,
        message_id="aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    )


def test_edge_advertisement_is_verified_and_uses_observed_peer_address():
    edge = parse_advertisement(
        _advertisement(),
        "192.0.2.41",
        PAIRING_KEY,
        now=1_700_000_005,
    )

    assert edge.device_id == "edge-001"
    assert edge.camera_id == "cam-001"
    assert edge.management_url == "http://192.0.2.41:8003"
    assert edge.recovery_url == "http://192.0.2.41:8002"
    assert edge.supported_profiles == ("hd", "fhd")


def test_edge_advertisement_rejects_wrong_key_tampering_and_replay():
    payload = _advertisement()

    for key, now in (("x" * 48, 1_700_000_001), (PAIRING_KEY, 1_700_000_100)):
        try:
            parse_advertisement(payload, "192.0.2.41", key, now=now)
        except ValueError:
            pass
        else:
            raise AssertionError("invalid discovery advertisement was accepted")

    tampered = json.loads(payload)
    tampered["camera_id"] = "cam-002"
    try:
        parse_advertisement(
            json.dumps(tampered).encode(),
            "192.0.2.41",
            PAIRING_KEY,
            now=1_700_000_001,
        )
    except ValueError:
        pass
    else:
        raise AssertionError("tampered discovery advertisement was accepted")


def test_pairing_endpoint_requires_key_and_writes_publish_configuration(tmp_path):
    token_file = tmp_path / "recovery.token"
    token_file.write_text(PAIRING_KEY + "\n", encoding="utf-8")
    session = PairingSession(
        config_path=tmp_path / "config.toml",
        pairing_key_file=token_file,
        device_id="edge-001",
        camera_id="cam-001",
    )
    client = TestClient(create_pairing_app(session))
    payload = {
        "device_id": "edge-001",
        "camera_id": "cam-001",
        "central_host": "192.0.2.10",
        "central_port": 8554,
        "backup_root": "/srv/ai-cctv-edge/recordings",
        "video_profile": "hd",
        "supported_profiles": ["hd", "fhd"],
        "publish_username": "cam-001",
        "publish_password": "s" * 48,
    }

    assert client.put("/internal/v1/pairing/complete", json=payload).status_code == 401
    response = client.put(
        "/internal/v1/pairing/complete",
        json=payload,
        headers={"Authorization": f"Bearer {PAIRING_KEY}"},
    )

    assert response.status_code == 200
    assert response.json()["status"] == "configured"
    assert session.completed.is_set()
    config = EdgeConfig.load(tmp_path / "config.toml")
    assert config.device_id == "edge-001"
    assert config.camera_id == "cam-001"
    assert config.rtsp.central_host == "192.0.2.10"
    assert config.rtsp.mode == "central_publish"
    assert config.backup.root == Path("/srv/ai-cctv-edge/recordings")
    assert (tmp_path / "publish.password").read_text(encoding="utf-8").strip() == (
        "s" * 48
    )
    assert (tmp_path / ".configured").is_file()


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit):
        return b'{"status":"configured","device_id":"edge-001","camera_id":"cam-001"}'


class _Opener:
    def __init__(self):
        self.request = None

    def open(self, request, timeout):
        self.request = request
        assert timeout == 10.0
        return _Response()


def test_configurator_delivers_one_time_publish_credential_without_url_secret(
    monkeypatch,
):
    opener = _Opener()
    monkeypatch.setattr("configurator.edge_pairing.build_opener", lambda *_: opener)
    edge = parse_advertisement(
        _advertisement(),
        "192.0.2.41",
        PAIRING_KEY,
        now=1_700_000_001,
    )

    result = complete_edge_pairing(
        edge,
        pairing_key=PAIRING_KEY,
        server_response={
            "camera_id": "cam-001",
            "publish_credentials": {
                "username": "cam-001",
                "password": "s" * 48,
            },
        },
        central_host="192.0.2.10",
        central_port=8554,
        video_profile="hd",
        backup_root="/srv/ai-cctv-edge/recordings",
    )

    assert result["status"] == "configured"
    assert opener.request.full_url == (
        "http://192.0.2.41:8003/internal/v1/pairing/complete"
    )
    assert PAIRING_KEY not in opener.request.full_url
    assert opener.request.get_header("Authorization") == f"Bearer {PAIRING_KEY}"
    body = json.loads(opener.request.data)
    assert body["publish_password"] == "s" * 48
    assert body["central_host"] == "192.0.2.10"
    assert body["backup_root"] == "/srv/ai-cctv-edge/recordings"
