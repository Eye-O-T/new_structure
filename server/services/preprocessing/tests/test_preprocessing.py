# 추적 이벤트의 상태 전환과 전처리 설정·스냅샷·내부 호출 계약을 모델 실행 없이 확인한다.
from pathlib import Path

import pytest

from server.services.preprocessing.app.data_client import DataClient
from server.services.preprocessing.app.event_state import TrackState
from server.services.preprocessing.app.pipeline import CameraWorker
from server.services.preprocessing.app.settings import Settings


MEDIA_READ_USERNAME = "inference-reader"
MEDIA_READ_PASSWORD = "r" * 40


# 같은 인물의 연속 감지는 등장 이벤트를 반복하지 않고 마지막 감지 후 제한 시간에 이탈한다.
def test_track_state_emits_appearance_once_and_disappearance_after_timeout():
    state = TrackState(disappear_seconds=3.0)
    first = state.update([{"person_id": 7, "confidence": 0.9}], 10.0)
    assert [(event.event_type, event.person_id) for event in first] == [
        ("person_appeared", "7")
    ]

    assert state.update([{"person_id": 7, "confidence": 0.8}], 11.0) == []
    assert state.update([], 13.9) == []
    gone = state.update([], 14.0)
    assert [(event.event_type, event.person_id) for event in gone] == [
        ("person_disappeared", "7")
    ]


# 한 인물의 감지가 계속되어도 다른 인물의 이탈 타이머는 독립적으로 진행한다.
def test_track_state_handles_independent_people():
    state = TrackState(disappear_seconds=1.0)
    appeared = state.update(
        [
            {"person_id": "a", "confidence": 0.7},
            {"person_id": "b", "confidence": 0.8},
        ],
        0.0,
    )
    assert {event.person_id for event in appeared} == {"a", "b"}
    gone = state.update([{"person_id": "b", "confidence": 0.8}], 1.0)
    assert [event.person_id for event in gone] == ["a"]


# 스냅샷 경로는 Data API의 최상위 필드로 보내고 전처리가 통합 인물 ID를 만들어내지 않는다.
def test_event_sends_snapshot_as_top_level_data_field(tmp_path):
    class Client:
        def __init__(self):
            self.payload = None

        def create_event(self, payload):
            self.payload = payload

    client = Client()
    settings = Settings(
        data_service_url="http://data",
        internal_service_token="token",
        rtsp_base_url="rtsp://media",
        media_read_username=MEDIA_READ_USERNAME,
        media_read_password=MEDIA_READ_PASSWORD,
        snapshots_root=tmp_path,
        model_path=tmp_path / "model.pt",
        device="cpu",
        confidence=0.4,
        analysis_fps=5,
        disappear_seconds=3,
        refresh_seconds=15,
        inference_enabled=False,
    )
    worker = CameraWorker(
        {"camera_id": "cam-001"}, settings, client, tracker_factory=lambda *_: None
    )
    worker._event(
        "person_appeared",
        person_id="7",
        confidence=0.9,
        snapshot_path="cam-001/2026/08/22/frame.jpg",
    )
    assert client.payload["snapshot_path"] == "cam-001/2026/08/22/frame.jpg"
    assert "snapshot_path" not in client.payload["metadata"]
    assert client.payload["person_id"] == "7"
    assert client.payload["global_person_id"] is None
    assert "track_id" not in client.payload


# 중앙의 AI 입력 장애·복구는 Edge 카메라 입력 이벤트와 구분하고 반복 보고를 줄인다.
def test_inference_stream_events_do_not_impersonate_edge_ingest(tmp_path):
    class Client:
        def __init__(self):
            self.events = []
            self.statuses = []

        def create_event(self, payload):
            self.events.append(payload)

        def set_camera_status(self, camera_id, status):
            self.statuses.append((camera_id, status))

    client = Client()
    settings = Settings(
        data_service_url="http://data",
        internal_service_token="token",
        rtsp_base_url="rtsp://media",
        media_read_username=MEDIA_READ_USERNAME,
        media_read_password=MEDIA_READ_PASSWORD,
        snapshots_root=tmp_path,
        model_path=tmp_path / "model.pt",
        device="cpu",
        confidence=0.4,
        analysis_fps=5,
        disappear_seconds=3,
        refresh_seconds=15,
        inference_enabled=False,
    )
    worker = CameraWorker({"camera_id": "cam-001"}, settings, client)

    worker._inference_stream_lost("rtsp_open_failed")
    worker._inference_stream_lost("rtsp_open_failed")
    worker._inference_stream_restored()
    worker._inference_stream_restored()

    assert [event["event_type"] for event in client.events] == [
        "inference_stream_lost",
        "inference_stream_restored",
    ]
    assert client.events[0]["metadata"]["reason"] == "rtsp_open_failed"
    assert client.statuses == [("cam-001", "offline")]


# 환경변수의 추론 값이 없으면 공유 YAML을 읽고 내부 인증은 전용 토큰을 우선한다.
def test_settings_load_inference_values_from_shared_config(tmp_path, monkeypatch):
    config = tmp_path / "config.yaml"
    config.write_text(
        """schema_version: 1
inference:
  enabled: false
  model_path: /models/custom.onnx
  device: cpu
  confidence_threshold: 0.55
  analysis_fps: 2
  disappear_seconds: 4
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_CCTV_CONFIG_FILE", str(config))
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "token")
    monkeypatch.setenv("DATA_INFERENCE_TOKEN", "scoped-inference-token")
    monkeypatch.setenv("MEDIA_READ_USERNAME", MEDIA_READ_USERNAME)
    monkeypatch.setenv("MEDIA_READ_PASSWORD", MEDIA_READ_PASSWORD)
    for name in (
        "MODEL_PATH",
        "INFERENCE_DEVICE",
        "INFERENCE_CONFIDENCE",
        "ANALYSIS_FPS",
        "DISAPPEAR_SECONDS",
        "INFERENCE_ENABLED",
    ):
        monkeypatch.delenv(name, raising=False)
    settings = Settings.from_env()
    assert settings.internal_service_token == "scoped-inference-token"
    assert settings.media_read_username == MEDIA_READ_USERNAME
    assert settings.media_read_password == MEDIA_READ_PASSWORD
    assert settings.model_path == Path("/models/custom.onnx")
    assert settings.device == "cpu"
    assert settings.confidence == 0.55
    assert settings.analysis_fps == 2
    assert settings.disappear_seconds == 4
    assert settings.inference_enabled is False
    monkeypatch.delenv("DATA_INFERENCE_TOKEN")
    assert Settings.from_env().internal_service_token == "token"


# 자격 증명과 스트림 경로의 특수문자를 각각 인코딩해 RTSP URL 구분자로 오해하지 않게 한다.
def test_inference_rtsp_url_quotes_read_credentials_and_stream_path(tmp_path):
    settings = Settings(
        data_service_url="http://data",
        internal_service_token="token",
        rtsp_base_url="rtsp://media:8554/root",
        media_read_username="reader:name@example",
        media_read_password="p@ss:/?#[]" + "x" * 32,
        snapshots_root=tmp_path,
        model_path=tmp_path / "model.pt",
        device="cpu",
        confidence=0.4,
        analysis_fps=5,
        disappear_seconds=3,
        refresh_seconds=15,
        inference_enabled=False,
    )
    settings.validate()

    assert settings.rtsp_source_url("floor 1/cam#1") == (
        "rtsp://reader%3Aname%40example:"
        "p%40ss%3A%2F%3F%23%5B%5Dxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        "@media:8554/root/floor%201/cam%231"
    )


# 전용 읽기 계정·충분한 비밀번호·올바른 실행 장치가 없으면 시작 설정 검증에 실패한다.
def test_inference_requires_dedicated_rtsp_read_credentials(tmp_path):
    common = {
        "data_service_url": "http://data",
        "internal_service_token": "token",
        "rtsp_base_url": "rtsp://media",
        "snapshots_root": tmp_path,
        "model_path": tmp_path / "model.pt",
        "device": "cpu",
        "confidence": 0.4,
        "analysis_fps": 5,
        "disappear_seconds": 3,
        "refresh_seconds": 15,
        "inference_enabled": False,
    }
    with pytest.raises(ValueError, match="MEDIA_READ_USERNAME"):
        Settings(
            media_read_username="",
            media_read_password=MEDIA_READ_PASSWORD,
            **common,
        ).validate()
    with pytest.raises(ValueError, match="MEDIA_READ_PASSWORD"):
        Settings(
            media_read_username=MEDIA_READ_USERNAME,
            media_read_password="short",
            **common,
        ).validate()
    with pytest.raises(ValueError, match="INFERENCE_DEVICE"):
        Settings(
            media_read_username=MEDIA_READ_USERNAME,
            media_read_password=MEDIA_READ_PASSWORD,
            **{**common, "device": "gpu-zero"},
        ).validate()


# 내부 서비스 요청과 MediaMTX 훅이 외부 프록시 환경을 경유하지 않도록 설정을 확인한다.
def test_internal_inference_and_media_calls_ignore_environment_proxies():
    client = DataClient("http://data", "token")
    try:
        assert client._client._trust_env is False
    finally:
        client.close()

    hook = Path("server/services/mediamtx/recording-complete-hook.sh").read_text(
        encoding="utf-8"
    )
    assert "--noproxy '*'" in hook
