import configparser
import json
import os
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from ai_cctv_edge.config import EdgeConfig
from ai_cctv_edge.cli import _publish_password_from_file, export_auth_token, setup
from ai_cctv_edge.control import (
    ActivationResult,
    CameraMode,
    LocalCapabilityProbe,
    LocalProfileRuntime,
    VideoCapabilities,
    _parse_primary_camera_modes,
    create_control_app,
)
from ai_cctv_edge.monitoring import (
    CameraInputWatchdog,
    PowerEventDetector,
    PowerReading,
    ResourceSnapshot,
)
from ai_cctv_edge.pipeline import (
    build_gstreamer_command,
    build_profile_probe_command,
)
from ai_cctv_edge.retention import enforce_retention
from ai_cctv_edge.recovery import _capture_may_write, _segment_start, create_app
from ai_cctv_edge.runner import CameraInputLostError, EdgeRunner
from ai_cctv_edge.state import EventJournal, ProfileSelectionStore, RuntimeStatusStore


def write_config(
    path: Path,
    camera_id: str = "cam-001",
    mode: str = "central_pull",
    supported_profiles: tuple[str, ...] = ("hd", "fhd"),
) -> None:
    quoted_profiles = ", ".join(f'"{item}"' for item in supported_profiles)
    path.write_text(
        f'''schema_version = 1
device_id = "edge-001"
camera_id = "{camera_id}"
[video]
profile = "hd"
width = 1280
height = 720
fps = 30
bitrate_kbps = 2000
encoder = "x264enc"
supported_profiles = [{quoted_profiles}]
[rtsp]
mode = "{mode}"
central_host = "127.0.0.1"
central_port = 8554
edge_port = 8554
username = "cam-001"
password_file = "{(path.parent / "publish.password").as_posix()}"
mediamtx_binary = "/bin/true"
[backup]
root = "{(path.parent / "recordings").as_posix()}"
segment_seconds = 10
max_bytes = 100
max_age_hours = 1
[recovery]
bind_host = "127.0.0.1"
port = 8002
token_file = "{(path.parent / "recovery.token").as_posix()}"
[control]
bind_host = "127.0.0.1"
port = 8003
token_file = "{(path.parent / "recovery.token").as_posix()}"
apply_timeout_seconds = 2
preflight_timeout_seconds = 1
[monitoring]
interval_seconds = 0.05
frame_timeout_seconds = 5
battery_low_percent = 20
battery_critical_percent = 10
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
    assert any("cam-001/2026/08/22" in part.replace("\\", "/") for part in command)
    assert config.video.profile == "hd"
    assert (config.video.width, config.video.height) == (1280, 720)
    assert "watchdog" in command
    assert "timeout=5000" in command


def test_edge_config_rejects_invalid_camera_id(tmp_path):
    path = tmp_path / "config.toml"
    write_config(path, "Bad/Path")
    with pytest.raises(ValueError, match="camera_id"):
        EdgeConfig.load(path)


def test_edge_publish_credential_handoff_checks_camera_identity_and_mode(tmp_path):
    handoff = tmp_path / "cam-001-publish.json"
    handoff.write_text(
        json.dumps(
            {
                "camera_id": "cam-001",
                "username": "cam-001",
                "password": "p" * 32,
            }
        ),
        encoding="utf-8",
    )
    os.chmod(handoff, 0o600)
    assert _publish_password_from_file(handoff, "cam-001") == "p" * 32

    mismatched = json.loads(handoff.read_text(encoding="utf-8"))
    mismatched["camera_id"] = "cam-002"
    handoff.write_text(json.dumps(mismatched), encoding="utf-8")
    os.chmod(handoff, 0o600)
    with pytest.raises(ValueError, match="camera ID"):
        _publish_password_from_file(handoff, "cam-001")


def test_setup_rejects_credentials_before_replacing_live_config(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "config.toml"
    config_path.write_text("existing-live-config\n", encoding="utf-8")
    state_root = tmp_path / "state"
    monkeypatch.setenv("AI_CCTV_EDGE_STATE_ROOT", str(state_root))
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    handoff = tmp_path / "wrong-camera.json"
    handoff.write_text(
        json.dumps(
            {
                "camera_id": "cam-002",
                "username": "cam-002",
                "password": "p" * 32,
            }
        ),
        encoding="utf-8",
    )
    os.chmod(handoff, 0o600)

    with pytest.raises(ValueError, match="camera ID"):
        setup(config_path, handoff)

    assert config_path.read_text(encoding="utf-8") == "existing-live-config\n"
    assert not (tmp_path / "publish.password").exists()
    assert not (tmp_path / "recovery.token").exists()
    assert not (tmp_path / ".configured").exists()
    assert not (state_root / "video-profile.json").exists()


def test_setup_writes_configured_marker_only_after_valid_credentials(
    tmp_path, monkeypatch
):
    config_path = tmp_path / "config.toml"
    state_root = tmp_path / "state"
    monkeypatch.setenv("AI_CCTV_EDGE_STATE_ROOT", str(state_root))
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    handoff = tmp_path / "cam-001-publish.json"
    handoff.write_text(
        json.dumps(
            {
                "camera_id": "cam-001",
                "username": "cam-001",
                "password": "p" * 32,
            }
        ),
        encoding="utf-8",
    )
    os.chmod(handoff, 0o600)

    assert setup(config_path, handoff) == 0
    assert (tmp_path / ".configured").read_text(encoding="utf-8") == "configured\n"
    assert EdgeConfig.load(config_path).camera_id == "cam-001"


def test_export_auth_token_creates_private_one_time_handoff(tmp_path, capsys):
    config_path = tmp_path / "config.toml"
    write_config(config_path)
    token = "t" * 48
    (tmp_path / "recovery.token").write_text(token + "\n", encoding="utf-8")
    output = tmp_path / "handoff" / "edge-001-control.token"

    assert export_auth_token(config_path, output) == 0
    assert output.read_text(encoding="utf-8") == token + "\n"
    if os.name != "nt":
        assert output.stat().st_mode & 0o777 == 0o600
    assert token not in capsys.readouterr().out

    with pytest.raises(FileExistsError, match="already exists"):
        export_auth_token(config_path, output)


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


def test_retention_never_unlinks_the_newest_active_segment(tmp_path):
    completed = tmp_path / "20260822T080000.000000Z_000000.ts"
    active = tmp_path / "20260822T080000.000000Z_000001.ts"
    completed.write_bytes(b"a" * 80)
    active.write_bytes(b"b" * 80)
    os.utime(completed, (1, 1))
    os.utime(active, (2, 2))

    deleted = enforce_retention(
        tmp_path,
        max_bytes=1,
        max_age_hours=1,
        now=10_000,
        preserve_newest=True,
    )

    assert deleted == [completed]
    assert active.read_bytes() == b"b" * 80


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
    active_segment = segment.with_name("20260822T080000.000000Z_000001.ts")
    active_segment.write_bytes(b"still-being-written")
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
    assert len(response.json()["items"]) == 1
    item = response.json()["items"][0]
    assert item["relative_path"].endswith("000000.ts")
    downloaded = client.get(
        f"/v1/recovery/files/{item['relative_path']}",
        headers={"Authorization": f"Bearer {'r' * 48}"},
    )
    assert downloaded.content == b"mpeg-ts"
    assert (
        client.get(
            "/v1/recovery/files/2026/08/22/"
            "20260822T080000.000000Z_000001.ts",
            headers={"Authorization": f"Bearer {'r' * 48}"},
        ).status_code
        == 409
    )


def test_recovery_exposes_final_segment_after_capture_stops(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    write_config(path)
    (tmp_path / "recovery.token").write_text("r" * 48, encoding="utf-8")
    state_root = tmp_path / "state"
    state_root.mkdir()
    (state_root / "status.json").write_text(
        json.dumps({"camera_id": "cam-001", "state": "stopped"}),
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_CCTV_EDGE_STATE_ROOT", str(state_root))
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
    segment.write_bytes(b"finalized-on-stop")

    response = TestClient(create_app(path)).get(
        "/v1/recovery/manifest",
        params={
            "start": "2026-08-22T07:59:59Z",
            "end": "2026-08-22T08:00:11Z",
        },
        headers={"Authorization": f"Bearer {'r' * 48}"},
    )

    assert response.status_code == 200
    assert [item["relative_path"] for item in response.json()["items"]] == [
        "2026/08/22/20260822T080000.000000Z_000000.ts"
    ]


def test_recovery_treats_a_dead_capture_pid_as_finalized(tmp_path, monkeypatch):
    state_root = tmp_path / "state"
    state_root.mkdir()
    (state_root / "status.json").write_text(
        json.dumps(
            {
                "camera_id": "cam-001",
                "state": "running",
                "runner_pid": 1234,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("AI_CCTV_EDGE_STATE_ROOT", str(state_root))
    monkeypatch.setattr(
        "ai_cctv_edge.recovery.os.kill",
        lambda *_args: (_ for _ in ()).throw(ProcessLookupError()),
    )

    assert _capture_may_write("cam-001") is False


def test_recovery_segment_timestamp_uses_configured_duration(tmp_path):
    segment = tmp_path / "20260822T080000.000000Z_000002.ts"
    segment.write_bytes(b"segment")

    assert _segment_start(segment, 15).isoformat() == "2026-08-22T08:00:30+00:00"


def test_legacy_fhd_values_are_inferred_as_fhd(tmp_path):
    path = tmp_path / "legacy.toml"
    write_config(path)
    text = path.read_text(encoding="utf-8")
    text = text.replace('profile = "hd"\n', "")
    text = text.replace("width = 1280", "width = 1920")
    text = text.replace("height = 720", "height = 1080")
    text = text.replace("bitrate_kbps = 2000", "bitrate_kbps = 4000")
    path.write_text(text, encoding="utf-8")
    config = EdgeConfig.load(path)
    assert config.video.profile == "fhd"


def test_profile_probe_uses_exact_fhd_bitrate(tmp_path):
    path = tmp_path / "config.toml"
    write_config(path)
    config = EdgeConfig.load(path)
    fhd = config.video.with_profile("fhd")
    from dataclasses import replace

    command = build_profile_probe_command(replace(config, video=fhd))
    assert "video/x-raw,width=1920,height=1080,framerate=30/1" in command
    assert "bitrate=4000" in command


def test_capability_probe_filters_profiles_by_primary_camera_modes(
    tmp_path, monkeypatch
):
    path = tmp_path / "config.toml"
    write_config(path)
    config = EdgeConfig.load(path)
    camera_listing = """Available cameras
-----------------
0 : constrained [1920x1080 10-bit]
    Modes: 'RAW10' : 1280x720 [60.00 fps - crop]
                      1920x1080 [25.00 fps - crop]
1 : other [3840x2160 10-bit]
    Modes: 'RAW10' : 1920x1080 [60.00 fps - crop]
"""

    def fake_run(command, **_kwargs):
        if "--list-cameras" in command:
            return SimpleNamespace(returncode=0, stdout=camera_listing)
        return SimpleNamespace(returncode=0, stdout="")

    monkeypatch.setattr(
        "ai_cctv_edge.control.shutil.which",
        lambda command: f"/usr/bin/{command}",
    )
    monkeypatch.setattr("ai_cctv_edge.control.subprocess.run", fake_run)

    capabilities = LocalCapabilityProbe().inspect(config)

    assert capabilities.camera_available is True
    assert capabilities.encoder_available is True
    assert capabilities.supported_profiles == ("hd",)
    assert _parse_primary_camera_modes(camera_listing) == (
        CameraMode(1280, 720, 60.0),
        CameraMode(1920, 1080, 25.0),
    )


def test_capability_probe_accepts_nominal_fhd_30fps_mode(tmp_path):
    path = tmp_path / "config.toml"
    write_config(path)
    config = EdgeConfig.load(path)

    assert LocalCapabilityProbe._supported_profiles(
        config, (CameraMode(1920, 1080, 30.0),)
    ) == ("hd", "fhd")


class FakeCapabilityProbe:
    def __init__(self, supported=("hd", "fhd"), camera=True, encoder=True):
        self.result = VideoCapabilities(supported, camera, encoder)

    def inspect(self, _config):
        return self.result


class FakeProfileRuntime:
    def __init__(self, outcomes=None):
        self.current = "hd"
        self.persisted = "hd"
        self.current_generation = 0
        self.pending = None
        self.activations = []
        self.outcomes = list(outcomes or [ActivationResult("applied")])
        self.preflight_profiles = []

    def current_profile(self, _default_profile):
        return self.current

    def persisted_profile(self, _default_profile):
        return self.persisted

    def generation(self, _default_profile):
        return self.current_generation

    def preflight(self, candidate, _timeout_seconds):
        self.preflight_profiles.append(candidate.video.profile)

    def activate(self, profile, generation):
        self.pending = (profile, generation)
        self.activations.append(profile)

    def commit(self, profile, generation):
        assert self.current == profile
        self.persisted = profile
        self.current_generation = generation

    def clear_request(self, _generation):
        self.pending = None

    def wait_for(self, profile, generation, _timeout_seconds):
        assert self.pending == (profile, generation)
        outcome = self.outcomes.pop(0)
        if outcome.status == "applied":
            self.current = profile
            self.current_generation = generation
        return outcome


class FakePowerSensor:
    def read(self):
        return PowerReading(84, "external", False)


class FakeMetrics:
    def sample(self):
        return ResourceSnapshot(12.5, 34.5, 56.5)


def _management_client(
    tmp_path,
    runtime,
    supported=("hd", "fhd"),
    configured_profiles=None,
):
    path = tmp_path / "config.toml"
    write_config(
        path,
        supported_profiles=(configured_profiles or supported),
    )
    token = "e" * 48
    (tmp_path / "recovery.token").write_text(token, encoding="utf-8")
    app = create_control_app(
        path,
        state_root=tmp_path / "state",
        runtime_root=tmp_path / "run",
        capability_probe=FakeCapabilityProbe(supported=supported),
        profile_runtime=runtime,
        power_sensor=FakePowerSensor(),
        metrics=FakeMetrics(),
    )
    return TestClient(app), token


def test_management_api_auth_status_capabilities_and_apply(tmp_path):
    runtime = FakeProfileRuntime()
    client, token = _management_client(tmp_path, runtime)
    headers = {"Authorization": f"Bearer {token}"}
    assert client.get("/internal/v1/status").status_code == 401

    status = client.get("/internal/v1/status", headers=headers)
    assert status.status_code == 200
    assert status.json()["cpu_percent"] == 12.5
    assert status.json()["power_source"] == "external"
    capabilities = client.get("/internal/v1/capabilities/video", headers=headers).json()
    assert capabilities["supported_profiles"] == ["hd", "fhd"]
    assert capabilities["current_profile"] == "hd"

    applied = client.put(
        "/internal/v1/config/video-profile",
        json={"profile": "fhd"},
        headers=headers,
    )
    assert applied.status_code == 200
    assert applied.json() == {
        "status": "applied",
        "previous_profile": "hd",
        "current_profile": "fhd",
    }
    assert runtime.preflight_profiles == ["fhd"]
    events = client.get("/internal/v1/events", headers=headers).json()
    assert events["items"][-1]["event_type"] == "video_profile_changed"
    assert events["next_cursor"] == events["items"][-1]["event_id"]


def test_management_status_reports_probed_not_declared_profiles(tmp_path):
    runtime = FakeProfileRuntime()
    client, token = _management_client(
        tmp_path,
        runtime,
        supported=("hd",),
        configured_profiles=("hd", "fhd"),
    )

    status = client.get(
        "/internal/v1/status",
        headers={"Authorization": f"Bearer {token}"},
    ).json()

    assert status["capability_status"] == "available"
    assert status["supported_profiles"] == ["hd"]
    assert status["supported_video_profiles"] == ["hd"]


def test_management_status_does_not_reuse_stopped_or_stale_capture_values(
    tmp_path, monkeypatch
):
    runtime = FakeProfileRuntime()
    state_root = tmp_path / "state"
    client, token = _management_client(tmp_path, runtime)
    headers = {"Authorization": f"Bearer {token}"}
    store = RuntimeStatusStore(state_root)
    store.write(
        {
            "state": "stopped",
            "runner_pid": 1234,
            "camera_input": "online",
            "central_connection_status": "online",
        }
    )
    stopped = client.get("/internal/v1/status", headers=headers).json()
    assert stopped["capture_state"] == "stopped"
    assert stopped["camera_input"] == "offline"
    assert stopped["central_connection_status"] == "unknown"

    store.write(
        {
            "state": "running",
            "runner_pid": 999999,
            "camera_input": "online",
            "central_connection_status": "online",
        }
    )
    monkeypatch.setattr(
        "ai_cctv_edge.control.os.kill",
        lambda *_args: (_ for _ in ()).throw(ProcessLookupError()),
    )
    stale = client.get("/internal/v1/status", headers=headers).json()
    assert stale["capture_state"] == "stale"
    assert stale["camera_input"] == "offline"
    assert stale["central_connection_status"] == "unknown"


def test_profile_apply_rejects_unsupported_without_changing_pipeline(tmp_path):
    runtime = FakeProfileRuntime()
    client, token = _management_client(tmp_path, runtime, supported=("hd",))
    response = client.put(
        "/internal/v1/config/video-profile",
        json={"profile": "fhd"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 422
    assert response.json()["reason_code"] == "UNSUPPORTED_VIDEO_PROFILE"
    assert runtime.activations == []


def test_failed_profile_apply_rolls_back(tmp_path):
    runtime = FakeProfileRuntime(
        [
            ActivationResult("failed", "PIPELINE_START_FAILED"),
            ActivationResult("applied"),
        ]
    )
    client, token = _management_client(tmp_path, runtime)
    response = client.put(
        "/internal/v1/config/video-profile",
        json={"profile": "fhd"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 409
    assert response.json()["reason_code"] == "PIPELINE_START_FAILED"
    assert response.json()["current_profile"] == "hd"
    assert runtime.activations == ["fhd", "hd"]
    assert runtime.persisted == "hd"


def test_local_runtime_persists_only_after_verified_commit(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    write_config(path)
    config = EdgeConfig.load(path)
    state_root = tmp_path / "state"
    runtime_root = tmp_path / "run"
    selection = ProfileSelectionStore(state_root)
    selection.write("hd", 3)
    runtime = LocalProfileRuntime(
        config,
        selection,
        RuntimeStatusStore(state_root),
        runtime_root,
    )
    monkeypatch.setattr(runtime, "_runner_identity", lambda: (1234, "i" * 32))
    monkeypatch.setattr("ai_cctv_edge.control.os.kill", lambda *_args: None)

    runtime.activate("fhd", 4)
    assert selection.read("hd") == ("hd", 3)
    assert runtime.request_store.read()["profile"] == "fhd"
    runtime.commit("fhd", 4)
    assert selection.read("hd") == ("fhd", 4)
    assert runtime.request_store.read() is None


def test_runner_ignores_request_for_stale_instance(tmp_path):
    path = tmp_path / "config.toml"
    write_config(path)
    runner = EdgeRunner(
        path,
        state_root=tmp_path / "state",
        runtime_root=tmp_path / "run",
    )
    runner.selection_store.write("hd", 3)
    runner.request_store.write("fhd", 4, 1234, "stale-instance-id-0001")

    runner._load_effective_config()

    assert runner.config.video.profile == "hd"
    assert runner.profile_generation == 3
    assert runner.request_store.read() is None


def test_runner_consumes_request_for_its_current_instance(tmp_path):
    path = tmp_path / "config.toml"
    write_config(path)
    runner = EdgeRunner(
        path,
        state_root=tmp_path / "state",
        runtime_root=tmp_path / "run",
    )
    runner.selection_store.write("hd", 3)
    runner.request_store.write(
        "fhd",
        4,
        os.getpid(),
        runner.runner_instance_id,
    )

    runner._load_effective_config()

    assert runner.config.video.profile == "fhd"
    assert runner.profile_generation == 4


def test_runner_expires_uncommitted_profile_and_restores_persisted_selection(
    tmp_path,
):
    path = tmp_path / "config.toml"
    write_config(path)
    runner = EdgeRunner(
        path,
        state_root=tmp_path / "state",
        runtime_root=tmp_path / "run",
    )
    runner.selection_store.write("hd", 3)
    runner.request_store.write(
        "fhd",
        4,
        os.getpid(),
        runner.runner_instance_id,
    )
    runner._load_effective_config()
    assert runner.config.video.profile == "fhd"

    request = json.loads(runner.request_store.path.read_text(encoding="utf-8"))
    request["requested_monotonic"] = 0
    runner.request_store.path.write_text(json.dumps(request), encoding="utf-8")

    assert runner._expire_active_profile_request() is True
    assert runner.reload_event.is_set()
    assert runner.request_store.read() is None
    runner._load_effective_config()
    assert runner.config.video.profile == "hd"
    assert runner.profile_generation == 3


def test_runner_watchdog_uses_real_recording_activity_for_transitions(tmp_path):
    path = tmp_path / "config.toml"
    write_config(path)
    runner = EdgeRunner(
        path,
        state_root=tmp_path / "state",
        runtime_root=tmp_path / "run",
    )
    now = [0.0]
    runner.camera_watchdog = CameraInputWatchdog(5, clock=lambda: now[0])
    runner.active_backup_dir = tmp_path / "recordings-active"
    runner.active_backup_dir.mkdir()
    runner.active_segment_prefix = "20260822T080000.000000Z"
    segment = runner.active_backup_dir / (
        "20260822T080000.000000Z_000000.ts"
    )
    segment.write_bytes(b"first-frame")

    runner._monitor_camera_input()
    assert runner.camera_watchdog.status == "online"

    now[0] = 5.0
    with pytest.raises(CameraInputLostError, match="no_frame_timeout"):
        runner._monitor_camera_input()
    assert runner.camera_watchdog.status == "offline"

    segment.write_bytes(b"new-encoded-frame")
    runner._monitor_camera_input()
    assert runner.camera_watchdog.status == "online"
    assert [item["event_type"] for item in runner.events.read()] == [
        "camera_input_lost",
        "camera_input_restored",
    ]


def test_runner_restarts_publisher_without_stopping_capture(tmp_path, monkeypatch):
    path = tmp_path / "config.toml"
    write_config(path, mode="central_publish")
    runner = EdgeRunner(
        path,
        state_root=tmp_path / "state",
        runtime_root=tmp_path / "run",
    )
    capture = object()
    runner.capture = capture
    runner.central_connection_status = "online"
    runner.publisher = SimpleNamespace(returncode=1, poll=lambda: 1)
    monkeypatch.setattr(runner, "_write_status", lambda *_args, **_kwargs: None)

    runner._maintain_publisher()

    assert runner.capture is capture
    assert runner.publisher is None
    assert runner.central_connection_status == "offline"
    assert runner.events.read()[-1]["event_type"] == "central_connection_lost"

    starts = []
    runner.publisher_restart_at = 0
    monkeypatch.setattr(runner, "_start_publisher", lambda: starts.append(True))
    runner._maintain_publisher()
    assert starts == [True]
    assert runner.capture is capture


def test_publisher_spawn_failure_uses_backoff_without_stopping_capture(
    tmp_path, monkeypatch
):
    path = tmp_path / "config.toml"
    write_config(path, mode="central_publish")
    runner = EdgeRunner(
        path,
        state_root=tmp_path / "state",
        runtime_root=tmp_path / "run",
    )
    capture = object()
    runner.capture = capture
    monkeypatch.setattr(runner, "_write_status", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        runner,
        "_start_publisher",
        lambda: (_ for _ in ()).throw(OSError("process limit")),
    )

    runner._try_start_publisher(now=100.0)

    assert runner.capture is capture
    assert runner.publisher is None
    assert runner.central_connection_status == "offline"
    assert runner.publisher_restart_at == 101.0
    assert runner.publisher_delay == 2.0


def test_power_and_camera_watchdog_transition_events():
    detector = PowerEventDetector(low_percent=20, critical_percent=10)
    assert detector.consume(PowerReading(80, "external")) == []
    assert detector.consume(PowerReading(19, "battery")) == [
        "external_power_lost",
        "battery_low",
    ]
    assert detector.consume(PowerReading(9, "battery")) == ["battery_critical"]
    assert detector.consume(PowerReading(50, "external")) == ["external_power_restored"]

    now = [0.0]
    watchdog = CameraInputWatchdog(5, clock=lambda: now[0])
    now[0] = 5.0
    assert watchdog.poll() == "camera_input_lost"
    assert watchdog.poll() is None
    assert watchdog.observe_frame() == "camera_input_restored"


def test_event_journal_isolated_when_edge_is_reprovisioned_for_new_camera(tmp_path):
    legacy = EventJournal("cam-old", tmp_path)
    old_event = legacy.record("central_connection_lost")
    current = EventJournal("cam-new", tmp_path)
    new_event = current.record("central_connection_restored")

    assert [item["event_id"] for item in legacy.read()] == [old_event["event_id"]]
    assert [item["event_id"] for item in current.read()] == [new_event["event_id"]]
    assert legacy.path != current.path

    # A mixed legacy file from a previous release is filtered rather than
    # wedging the new camera's central event cursor.
    legacy_new_event = {**new_event, "event_id": "legacy-new-event"}
    legacy.legacy_path.write_text(
        json.dumps(old_event) + "\n" + json.dumps(legacy_new_event) + "\n",
        encoding="utf-8",
    )
    assert [item["event_id"] for item in current.read()] == [
        "legacy-new-event",
        new_event["event_id"],
    ]


def test_systemd_units_separate_capture_control_and_recovery_lifecycles():
    edge_root = Path(__file__).parents[1]
    units = {
        "ai-cctv-edge.service": " run",
        "ai-cctv-edge-control.service": " serve-control",
        "ai-cctv-edge-recovery.service": " serve-recovery",
    }
    for name, command_suffix in units.items():
        parser = configparser.ConfigParser(strict=False, interpolation=None)
        parser.read(edge_root / "systemd" / name, encoding="utf-8")
        assert {"Unit", "Service", "Install"}.issubset(parser.sections())
        assert command_suffix in parser["Service"]["ExecStart"]
        assert "network-online.target" not in parser["Unit"].get("After", "")
        assert (
            parser["Unit"]["ConditionPathExists"]
            == "/etc/ai-cctv-edge/.configured"
        )

    runner_source = (edge_root / "src/ai_cctv_edge/runner.py").read_text(
        encoding="utf-8"
    )
    assert "serve-recovery" not in runner_source
    build_script = (edge_root / "packaging/build_deb.sh").read_text(encoding="utf-8")
    assert 'systemd/"*.service' in build_script
    package_control = (edge_root / "packaging/debian/control").read_text(
        encoding="utf-8"
    )
    assert "python3 (>= 3.11), python3 (<< 3.12)" in package_control
    assert "gstreamer1.0-rtsp" in package_control
    postinst = (edge_root / "packaging/debian/postinst").read_text(encoding="utf-8")
    assert 'if [ -n "${2:-}" ]' in postinst
    assert "systemctl try-restart" in postinst
    assert "systemctl enable" not in postinst


def test_edge_package_metadata_and_reproducible_build_contract_are_consistent():
    edge_root = Path(__file__).parents[1]
    with (edge_root / "pyproject.toml").open("rb") as handle:
        project = tomllib.load(handle)["project"]
    package_version = project["version"]
    control = (edge_root / "packaging/debian/control").read_text(encoding="utf-8")
    assert f"Version: {package_version}\n" in control
    assert "Architecture: arm64" in control
    assert "rpicam-apps" in control

    build_script = (edge_root / "packaging/build_deb.sh").read_text(
        encoding="utf-8"
    )
    assert "SOURCE_DATE_EPOCH" in build_script
    assert "constraints.txt" in build_script
    assert "verify_deb.sh" in build_script
    assert "ai-cctv-edge_0.3.0_arm64" not in build_script

    postinst = (edge_root / "packaging/debian/postinst").read_text(encoding="utf-8")
    assert "ai-cctv-edge==0.3.0" not in postinst
    assert "--force-reinstall" in postinst

    constraints = {
        line.strip()
        for line in (edge_root / "packaging/constraints.txt")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and not line.startswith("#")
    }
    assert set(project["dependencies"]).issubset(constraints)
    assert "sniffio==1.3.1" in constraints

    deployment_doc = (
        edge_root.parent / "docs/operations/edge-deployment.md"
    ).read_text(encoding="utf-8")
    assert "AI_CCTV_CLI.exe edge-register" in deployment_doc
    assert "ai-cctv-server" not in deployment_doc

    root_readme = (edge_root.parent / "README.md").read_text(encoding="utf-8")
    handoff_steps = [
        "export-auth-token",
        "AI_CCTV_CLI.exe edge-register",
        "--publish-credentials-file",
    ]
    positions = [root_readme.find(step) for step in handoff_steps]
    assert all(position >= 0 for position in positions)
    assert positions == sorted(positions)
    assert "(docs/operations/edge-deployment.md)" in root_readme
