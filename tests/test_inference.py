from pathlib import Path

from server.services.inference.app.event_state import TrackState
from server.services.inference.app.pipeline import CameraWorker
from server.services.inference.app.settings import Settings


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
    assert settings.model_path == Path("/models/custom.onnx")
    assert settings.device == "cpu"
    assert settings.confidence == 0.55
    assert settings.analysis_fps == 2
    assert settings.disappear_seconds == 4
    assert settings.inference_enabled is False
