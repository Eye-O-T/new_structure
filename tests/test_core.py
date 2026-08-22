from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_cctv_core.config import AppConfig, CameraBootstrap, RecordingConfig, load_config
from ai_cctv_core.identifiers import safe_storage_path, validate_camera_id
from ai_cctv_core.time import format_utc, parse_utc


@pytest.mark.parametrize("camera_id", ["cam-001", "entrance_2", "a", "0" * 64])
def test_valid_camera_ids(camera_id):
    assert validate_camera_id(camera_id) == camera_id


@pytest.mark.parametrize("camera_id", ["", "Cam-001", "cam/001", "-camera", "a" * 65])
def test_invalid_camera_ids(camera_id):
    with pytest.raises(ValueError):
        validate_camera_id(camera_id)


def test_config_rejects_duplicate_camera_ids():
    with pytest.raises(ValueError, match="unique"):
        AppConfig(
            cameras=[
                CameraBootstrap(camera_id="cam-001", name="one"),
                CameraBootstrap(camera_id="cam-001", name="two"),
            ]
        )


def test_recording_segment_range():
    assert RecordingConfig(segment_seconds=10).segment_seconds == 10
    with pytest.raises(ValueError):
        RecordingConfig(segment_seconds=9)


def test_storage_path_rejects_traversal(tmp_path):
    assert safe_storage_path(tmp_path, "cam-001/a.mp4").is_relative_to(tmp_path)
    with pytest.raises(ValueError):
        safe_storage_path(tmp_path, "../outside.mp4")


def test_utc_round_trip():
    timestamp = datetime(2026, 8, 22, 8, 0, tzinfo=UTC)
    assert format_utc(timestamp) == "2026-08-22T08:00:00.000Z"
    assert parse_utc("2026-08-22T17:00:00+09:00") == timestamp


def test_naive_timestamp_is_rejected():
    with pytest.raises(ValueError, match="timezone"):
        parse_utc("2026-08-22T08:00:00")


def test_server_example_config_matches_core_schema():
    example = Path(__file__).resolve().parents[1] / "server/config/config.example.yaml"

    config = load_config(example)

    assert config.schema_version == 1
    assert config.server.public_http_port == 80
    assert config.recording.recovery_root == "/recovered"
    assert config.inference.event_pre_roll_seconds == 5
    assert [camera.camera_id for camera in config.cameras] == ["cam-001"]
