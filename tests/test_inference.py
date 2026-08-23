from pathlib import Path

import pytest

from server.services.inference.app.data_client import DataClient
from server.services.inference.app.event_state import TrackState
from server.services.inference.app.pipeline import CameraWorker
from server.services.inference.app.settings import Settings


MEDIA_READ_USERNAME = "inference-reader"
MEDIA_READ_PASSWORD = "r" * 40


def test_track_state_emits_appearance_once_and_disappearance_after_timeout():
    state = TrackState(disappear_seconds=3.0)
    first = state.update([{"track_id": 7, "confidence": 0.9}], 10.0)
    assert [(event.event_type, event.track_id) for event in first] == [
        ("person_appeared", "7")
    ]

    assert state.update([{"track_id": 7, "confidence": 0.8}], 11.0) == []
    assert state.update([], 13.9) == []
    gone = state.update([], 14.0)
    assert [(event.event_type, event.track_id) for event in gone] == [
        ("person_disappeared", "7")
    ]


def test_track_state_handles_independent_people():
    state = TrackState(disappear_seconds=1.0)
    appeared = state.update(
        [
            {"track_id": "a", "confidence": 0.7},
            {"track_id": "b", "confidence": 0.8},
        ],
        0.0,
    )
    assert {event.track_id for event in appeared} == {"a", "b"}
    gone = state.update([{"track_id": "b", "confidence": 0.8}], 1.0)
    assert [event.track_id for event in gone] == ["a"]


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
        track_id="7",
        confidence=0.9,
        snapshot_path="cam-001/2026/08/22/frame.jpg",
    )
    assert client.payload["snapshot_path"] == "cam-001/2026/08/22/frame.jpg"
    assert "snapshot_path" not in client.payload["metadata"]


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


def test_internal_inference_and_media_calls_ignore_environment_proxies():
    client = DataClient("http://data", "token")
    try:
        assert client._client._trust_env is False
    finally:
        client.close()

    hook = Path("server/mediamtx/recording-complete-hook.sh").read_text(
        encoding="utf-8"
    )
    assert "--noproxy '*'" in hook
