from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ai_cctv_edge.config import EdgeConfig
from ai_cctv_edge.pipeline import build_gstreamer_command
from ai_cctv_edge.retention import enforce_retention
from ai_cctv_edge.recovery import create_app


def write_config(
    path: Path, camera_id: str = "cam-001", mode: str = "central_pull"
) -> None:
    path.write_text(
        f'''schema_version = 1
device_id = "edge-001"
camera_id = "{camera_id}"
[video]
width = 1920
height = 1080
fps = 30
bitrate_kbps = 4000
encoder = "x264enc"
[rtsp]
mode = "{mode}"
central_host = "127.0.0.1"
central_port = 8554
edge_port = 8554
username = "cam-001"
password_file = "{path.parent / "publish.password"}"
mediamtx_binary = "/bin/true"
[backup]
root = "{path.parent / "recordings"}"
segment_seconds = 10
max_bytes = 100
max_age_hours = 1
[recovery]
bind_host = "127.0.0.1"
port = 8002
token_file = "{path.parent / "recovery.token"}"
''',
        encoding="utf-8",
    )


def test_edge_config_and_pipeline_use_camera_path_and_mpegts(tmp_path):
    path = tmp_path / "config.toml"
    write_config(path)
    config = EdgeConfig.load(path)
    command = build_gstreamer_command(config, "20260822T080000.000000Z")
    assert "muxer-factory=mpegtsmux" in command
    assert "location=rtmp://127.0.0.1:1935/cam-001" in command
    assert any("cam-001/2026/08/22" in part for part in command)


def test_edge_config_rejects_invalid_camera_id(tmp_path):
    path = tmp_path / "config.toml"
    write_config(path, "Bad/Path")
    with pytest.raises(ValueError, match="camera_id"):
        EdgeConfig.load(path)


def test_central_publish_uses_shared_memory_so_backup_does_not_block(tmp_path):
    path = tmp_path / "config.toml"
    write_config(path, mode="central_publish")
    command = build_gstreamer_command(EdgeConfig.load(path), "20260822T080000.000000Z")
    assert "shmsink" in command
    assert "wait-for-connection=false" in command
    assert not any("rtsp://" in item for item in command)


def test_retention_deletes_oldest_until_below_limit(tmp_path):
    first = tmp_path / "first.ts"
    second = tmp_path / "second.ts"
    first.write_bytes(b"a" * 80)
    second.write_bytes(b"b" * 80)
    first.touch()
    second.write_bytes(b"b" * 80)
    deleted = enforce_retention(tmp_path, max_bytes=100, max_age_hours=24)
    assert len(deleted) == 1
    assert sum(path.stat().st_size for path in tmp_path.glob("*.ts")) == 80


def test_recovery_manifest_and_file_require_token(tmp_path):
    path = tmp_path / "config.toml"
    write_config(path)
    (tmp_path / "recovery.token").write_text("r" * 48, encoding="utf-8")
    segment = (
        tmp_path
        / "recordings"
        / "cam-001"
        / "2026"
        / "08"
        / "22"
        / "20260822T080000.000000Z_000000.ts"
    )
    segment.parent.mkdir(parents=True)
    segment.write_bytes(b"mpeg-ts")
    client = TestClient(create_app(path))
    query = {
        "start": "2026-08-22T07:59:59Z",
        "end": "2026-08-22T08:00:11Z",
    }
    assert client.get("/v1/recovery/manifest", params=query).status_code == 401
    response = client.get(
        "/v1/recovery/manifest",
        params=query,
        headers={"Authorization": f"Bearer {'r' * 48}"},
    )
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item["relative_path"].endswith("000000.ts")
    downloaded = client.get(
        f"/v1/recovery/files/{item['relative_path']}",
        headers={"Authorization": f"Bearer {'r' * 48}"},
    )
    assert downloaded.content == b"mpeg-ts"
