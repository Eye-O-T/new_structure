# 실제 카메라 없이 Edge 설정·복구·품질 전환·프로세스 감시와 배포 패키지 계약을 검증한다.
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


# 테스트별 임시 경로를 사용하는 HD 기본 설정을 만들고 송출 방식·지원 품질만 바꿀 수 있게 한다.
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


# 카메라 ID가 송출·녹화 경로를 결정하고 MPEG-TS 분할 녹화와 입력 감시가 활성화돼야 한다.
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


# 경로에 쓰기 부적합한 카메라 ID는 파이프라인 생성 전에 설정 읽기에서 거부한다.
def test_edge_config_rejects_invalid_camera_id(tmp_path):
    path = tmp_path / "config.toml"
    write_config(path, "Bad/Path")
    with pytest.raises(ValueError, match="camera_id"):
        EdgeConfig.load(path)


# 파일의 카메라 ID가 대상과 일치하는 자격 증명만 읽고 다른 카메라의 비밀번호는 거부한다.
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


# 잘못된 전달 파일은 기존 실행 설정을 보존하고 새 비밀 파일·완료 표식을 남기지 않아야 한다.
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


# 유효한 자격 증명으로 설정을 완성한 뒤에만 서비스 시작을 허용하는 표식을 생성한다.
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


# 토큰 전달 파일은 비공개로 새로 생성하고 화면 출력이나 기존 파일 덮어쓰기를 허용하지 않는다.
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


# 중앙 송출이 연결되지 않아도 캡처·백업 경로가 공유 메모리 소비자를 기다리지 않아야 한다.
def test_central_publish_uses_shared_memory_so_backup_does_not_block(tmp_path):
    path = tmp_path / "config.toml"
    write_config(path, mode="central_publish")
    command = build_gstreamer_command(EdgeConfig.load(path), "20260822T080000.000000Z")
    assert "shmsink" in command
    assert "wait-for-connection=false" in command
    assert not any("rtsp://" in item for item in command)


# 저장 용량 제한을 넘으면 일부 오래된 조각을 정리해 총량을 제한 아래로 내려야 한다.
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


# 용량 제한을 만족할 수 없더라도 기록 중일 수 있는 최신 조각은 삭제하지 않는다.
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


# 인증한 클라이언트는 완료된 조각만 조회·다운로드하고 기록 중인 최신 파일은 거부받아야 한다.
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


# 캡처가 멈춘 뒤에는 최신 조각도 더 이상 쓰이지 않으므로 복구 목록에 포함한다.
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


# 상태 파일이 running이어도 실제 프로세스가 없으면 캡처의 추가 쓰기가 없다고 판단한다.
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


# 파일 순번에 설정한 조각 길이를 곱해 복구 구간의 시작 시각을 계산해야 한다.
def test_recovery_segment_timestamp_uses_configured_duration(tmp_path):
    segment = tmp_path / "20260822T080000.000000Z_000002.ts"
    segment.write_bytes(b"segment")

    assert _segment_start(segment, 15).isoformat() == "2026-08-22T08:00:30+00:00"


# 품질 이름이 없던 구형 설정도 해상도·비트레이트에서 기존 FHD 선택을 복원한다.
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


# 사전 품질 검사는 실제 적용할 FHD 해상도·프레임률·비트레이트로 실행돼야 한다.
def test_profile_probe_uses_exact_fhd_bitrate(tmp_path):
    path = tmp_path / "config.toml"
    write_config(path)
    config = EdgeConfig.load(path)
    fhd = config.video.with_profile("fhd")
    from dataclasses import replace

    command = build_profile_probe_command(replace(config, video=fhd))
    assert "video/x-raw,width=1920,height=1080,framerate=30/1" in command
    assert "bitrate=4000" in command


# 다른 카메라의 고성능 모드가 섞여도 첫 카메라가 지원하는 모드로만 품질을 결정한다.
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


# FHD 30fps를 정확히 지원하는 경계 모드도 HD·FHD 선택을 제공해야 한다.
def test_capability_probe_accepts_nominal_fhd_30fps_mode(tmp_path):
    path = tmp_path / "config.toml"
    write_config(path)
    config = EdgeConfig.load(path)

    assert LocalCapabilityProbe._supported_profiles(
        config, (CameraMode(1920, 1080, 30.0),)
    ) == ("hd", "fhd")


# 하드웨어 탐색 없이 지원 품질·카메라·인코더 존재 여부를 고정해 제공한다.
class FakeCapabilityProbe:
    def __init__(self, supported=("hd", "fhd"), camera=True, encoder=True):
        self.result = VideoCapabilities(supported, camera, encoder)

    def inspect(self, _config):
        return self.result


# 적용 대기·성공·실패·commit을 분리해 실제 파이프라인 없이 품질 트랜잭션을 재현한다.
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


# 상태 API가 전원 정보를 직렬화하는지 확인할 고정 센서값을 제공한다.
class FakePowerSensor:
    def read(self):
        return PowerReading(84, "external", False)


# CPU·메모리·저장소 지표를 고정해 호스트의 실제 부하와 테스트 결과를 분리한다.
class FakeMetrics:
    def sample(self):
        return ResourceSnapshot(12.5, 34.5, 56.5)


# 인증 토큰과 하드웨어 대역을 주입한 관리 API를 만들며 선언·탐색 품질 차이도 재현한다.
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


# 관리 인증부터 센서값·지원 품질 조회, 품질 적용과 성공 이벤트까지 전체 요청을 확인한다.
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


# 설정에 선언된 품질보다 실제 하드웨어 탐색 결과를 우선해 지원 목록을 응답한다.
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


# 종료되거나 사라진 캡처의 과거 online 값을 현재 연결 상태처럼 노출하지 않아야 한다.
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


# 지원하지 않는 품질은 활성화 요청을 만들기 전에 거부해야 한다.
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


# 새 품질 적용 실패 후 이전 품질을 다시 활성화하고 영구 선택값은 유지한다.
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


# activate는 임시 요청만 남기며 commit이 성공해야 저장된 품질과 세대를 교체한다.
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


# 이전 실행 인스턴스를 대상으로 남은 요청은 새 runner가 적용하지 않고 제거한다.
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


# 현재 PID와 인스턴스 ID가 일치하는 요청만 실행 품질과 세대에 반영한다.
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


# 관리 프로세스가 commit하지 못한 임시 품질은 제한 시간 뒤 저장된 선택으로 되돌린다.
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


# 조각 파일의 실제 증가와 가상 시계로 입력 중단·복구 이벤트가 한 번씩 생기는지 확인한다.
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


# 중앙 송출 프로세스만 실패한 경우 캡처 객체를 유지한 채 송출 재시작을 예약한다.
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


# 송출 프로세스 생성 실패는 캡처를 종료하지 않고 증가하는 지연으로 재시도한다.
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


# 임계값과 입력 타임아웃 경계를 넘을 때만 장애·복구 이벤트를 내고 반복 상태는 생략한다.
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


# 같은 Edge 저장소를 새 카메라에 재사용해도 이전 카메라의 이벤트가 섞이지 않아야 한다.
def test_event_journal_isolated_when_edge_is_reprovisioned_for_new_camera(tmp_path):
    legacy = EventJournal("cam-old", tmp_path)
    old_event = legacy.record("central_connection_lost")
    current = EventJournal("cam-new", tmp_path)
    new_event = current.record("central_connection_restored")

    assert [item["event_id"] for item in legacy.read()] == [old_event["event_id"]]
    assert [item["event_id"] for item in current.read()] == [new_event["event_id"]]
    assert legacy.path != current.path

    # 구형 혼합 일지도 카메라별로 걸러 새 카메라의 이벤트 조회가 막히지 않아야 한다.
    legacy_new_event = {**new_event, "event_id": "legacy-new-event"}
    legacy.legacy_path.write_text(
        json.dumps(old_event) + "\n" + json.dumps(legacy_new_event) + "\n",
        encoding="utf-8",
    )
    assert [item["event_id"] for item in current.read()] == [
        "legacy-new-event",
        new_event["event_id"],
    ]


# 캡처·제어·복구 서비스가 독립 진입점을 사용하고 패키지 갱신이 서비스 활성화를 강제하지 않아야 한다.
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


# 패키지 버전·아키텍처·고정 의존성과 수동 자격 증명 전달 문서의 순서를 함께 검사한다.
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

    root_readme = (edge_root.parent / "README.md").read_text(encoding="utf-8")
    deployment_doc = (edge_root / "README.md").read_text(encoding="utf-8")
    assert "AI_CCTV_CLI.exe edge-register" in deployment_doc
    assert "ai-cctv-server" not in deployment_doc

    handoff_guide = deployment_doc.split("## 수동 연결", 1)[1]
    handoff_steps = [
        "export-auth-token",
        "AI_CCTV_CLI.exe edge-register",
        "--publish-credentials-file",
    ]
    positions = [handoff_guide.find(step) for step in handoff_steps]
    assert all(position >= 0 for position in positions)
    assert positions == sorted(positions)
    assert "(docs/architecture.md)" in root_readme
    assert "## 운영과 백업" in root_readme
