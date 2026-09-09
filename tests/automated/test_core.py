# 공용 설정·식별자·경로·UTC 변환의 경계값과 서버 예제 설정의 호환성을 확인한다.
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_cctv_core.config import (
    AppConfig,
    CameraBootstrap,
    InferenceConfig,
    RecordingConfig,
    load_config,
)
from ai_cctv_core.identifiers import safe_storage_path, validate_camera_id
from ai_cctv_core.time import format_utc, parse_utc


# 허용 문자와 최대 길이의 ID는 정규화로 변형하지 않고 그대로 통과해야 한다.
@pytest.mark.parametrize("camera_id", ["cam-001", "entrance_2", "a", "0" * 64])
def test_valid_camera_ids(camera_id):
    assert validate_camera_id(camera_id) == camera_id


# 경로 구분자·대문자·빈 값·길이 초과는 저장 경로에 사용되기 전에 거부한다.
@pytest.mark.parametrize("camera_id", ["", "Cam-001", "cam/001", "-camera", "a" * 65])
def test_invalid_camera_ids(camera_id):
    with pytest.raises(ValueError):
        validate_camera_id(camera_id)


# 부트스트랩 목록에 같은 카메라가 두 번 등록되는 설정을 차단한다.
def test_config_rejects_duplicate_camera_ids():
    with pytest.raises(ValueError, match="unique"):
        AppConfig(
            cameras=[
                CameraBootstrap(camera_id="cam-001", name="one"),
                CameraBootstrap(camera_id="cam-001", name="two"),
            ]
        )


# Edge 관리·복구 주소는 검증 후 루트 슬래시를 제거한 같은 형식으로 보관한다.
def test_camera_bootstrap_accepts_safe_edge_management_url():
    camera = CameraBootstrap(
        camera_id="cam-001",
        name="Entrance",
        edge_device_id="edge-001",
        edge_management_url="https://192.0.2.10:8003/",
        edge_recovery_url="https://192.0.2.10:8002/",
    )
    assert camera.edge_management_url == "https://192.0.2.10:8003"
    assert camera.edge_recovery_url == "https://192.0.2.10:8002"


# 관리 주소에 다른 프로토콜·자격 증명·쿼리·경로 우회가 섞이는 경우를 검사한다.
@pytest.mark.parametrize(
    "url",
    [
        "rtsp://edge.example:8554",
        "https://user:secret@edge.example",
        "https://edge.example/?token=secret",
        "https://edge.example/base/../admin",
    ],
)
def test_camera_bootstrap_rejects_unsafe_edge_management_url(url):
    with pytest.raises(ValueError, match="Edge service URL"):
        CameraBootstrap(
            camera_id="cam-001",
            name="Entrance",
            edge_management_url=url,
        )


# 녹화 분할 길이의 최소 허용값과 그 직전 값을 함께 확인한다.
def test_recording_segment_range():
    assert RecordingConfig(segment_seconds=10).segment_seconds == 10
    with pytest.raises(ValueError):
        RecordingConfig(segment_seconds=9)


# 실행 장치는 명시적인 CPU 또는 CUDA 선택을 허용하며 임의 별칭은 거부한다.
def test_inference_device_contract_supports_explicit_cpu_and_cuda_selection():
    assert InferenceConfig(device="cpu").device == "cpu"
    assert InferenceConfig(device="cuda:0").device == "cuda:0"
    with pytest.raises(ValueError):
        InferenceConfig(device="gpu-zero")


# 상대 경로를 결합한 결과가 지정 저장소 밖으로 벗어나지 않아야 한다.
def test_storage_path_rejects_traversal(tmp_path):
    assert safe_storage_path(tmp_path, "cam-001/a.mp4").is_relative_to(tmp_path)
    with pytest.raises(ValueError):
        safe_storage_path(tmp_path, "../outside.mp4")


# UTC 출력 형식과 서로 다른 시간대가 나타내는 동일 순간의 변환을 확인한다.
def test_utc_round_trip():
    timestamp = datetime(2026, 8, 22, 8, 0, tzinfo=UTC)
    assert format_utc(timestamp) == "2026-08-22T08:00:00.000Z"
    assert parse_utc("2026-08-22T17:00:00+09:00") == timestamp


# 시간대 없는 입력을 서버 현지 시간으로 추측해 사건 시각을 바꾸지 않아야 한다.
def test_naive_timestamp_is_rejected():
    with pytest.raises(ValueError, match="timezone"):
        parse_utc("2026-08-22T08:00:00")


# 배포 예제가 실제 공용 스키마와 기본 조회·녹화 설정으로 읽히는지 확인한다.
def test_server_example_config_matches_core_schema():
    example = Path(__file__).resolve().parents[2] / "server/config/config.example.yaml"

    config = load_config(example)

    assert config.schema_version == 1
    assert config.server.public_http_port == 80
    assert config.recording.recovery_root == "/recordings/recovered"
    assert config.inference.event_pre_roll_seconds == 5
    assert [camera.camera_id for camera in config.cameras] == ["cam-001"]
