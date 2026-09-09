# 실제 Edge와 설치 도우미의 검색·페어링 계약을 고정 시각과 모의 HTTP로 검증한다.
import json
from pathlib import Path

from fastapi.testclient import TestClient

from ai_cctv_edge.config import EdgeConfig
from ai_cctv_edge.pairing import (
    PairingSession,
    build_advertisement,
    create_pairing_app,
)
from server.setup.install_helper.edge_discovery import parse_advertisement
from server.setup.install_helper.edge_pairing import (
    complete_edge_pairing,
    probe_edge_connection,
)


PAIRING_KEY = "p" * 48


# 서명 검증과 재전송 제한을 재현할 수 있게 ID와 전송 시각을 고정한 광고를 만든다.
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


# 관리 주소는 광고가 주장하는 호스트 대신 실제 패킷 송신 주소에서 구성해야 한다.
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


# 잘못된 서명키·본문 변조·허용 시간 경과는 모두 신뢰할 수 없는 광고로 거부한다.
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


# 인증된 연결 완료 요청만 송출 설정·비밀번호 파일·설정 완료 표식을 기록할 수 있다.
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


# urllib 응답의 문맥 관리자와 read 인터페이스만 제공하는 성공 응답 대역이다.
class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit):
        return b'{"status":"configured","device_id":"edge-001","camera_id":"cam-001"}'


# 실제 네트워크 연결 대신 최종 요청을 보관해 URL·헤더·본문을 검사한다.
class _Opener:
    def __init__(self):
        self.request = None

    def open(self, request, timeout):
        self.request = request
        assert timeout == 10.0
        return _Response()


# 일회성 송출 자격 증명은 본문으로, 페어링 키는 헤더로 전달하고 URL에는 싣지 않는다.
def test_install_helper_delivers_one_time_publish_credential_without_url_secret(
    monkeypatch,
):
    opener = _Opener()
    monkeypatch.setattr(
        "server.setup.install_helper.edge_pairing.build_opener", lambda *_: opener
    )
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


# 등록 전 관리 API가 응답하는 기기·카메라 식별자가 발견한 Edge와 맞는지 확인한다.
def test_install_helper_probes_discovered_edge_identity_before_registration(
    monkeypatch,
):
    opener = _Opener()
    monkeypatch.setattr(
        "server.setup.install_helper.edge_pairing.build_opener", lambda *_: opener
    )
    edge = parse_advertisement(
        _advertisement(),
        "192.0.2.41",
        PAIRING_KEY,
        now=1_700_000_001,
    )

    class HealthResponse(_Response):
        def read(self, _limit):
            return b'{"status":"pairing","device_id":"edge-001","camera_id":"cam-001"}'

    opener.open = lambda request, timeout: HealthResponse()
    result = probe_edge_connection(edge)

    assert result["status"] == "pairing"
    assert result["management_url"] == "http://192.0.2.41:8003"
    assert result["supported_profiles"] == ["hd", "fhd"]
