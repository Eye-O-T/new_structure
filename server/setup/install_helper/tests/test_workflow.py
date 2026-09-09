"""설치 전에 모든 입력을 검사하고 기존 운영 파일을 보호하는 계약을 검증한다."""

from dataclasses import asdict, replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_cctv_core.config import AppConfig, CameraBootstrap, write_config_atomic
from server.setup import config_core
from server.setup.config_core import InstallRequest
from server.setup.install_helper import workflow
from server.setup.install_helper.compose_adapter import Prerequisite
from server.setup.validation import read_deployment_env


def test_cli_initialization_protects_existing_keys_unless_explicit_reset(
    request_files, available_dependencies, capsys
):
    from server.setup.install_helper import cli

    request = request_files
    password_file = request.data_root.parent / "password.txt"
    password_file.write_text(request.admin_password, encoding="utf-8")
    arguments = [
        "--server-dir",
        str(request.server_dir),
        "init",
        "--data-root",
        str(request.data_root),
        "--admin-password-file",
        str(password_file),
        "--model",
        str(request.model_path),
    ]
    assert cli.main(arguments) == 0
    secrets_file = request.data_root / "secrets" / "external.env"
    original = secrets_file.read_bytes()
    assert cli.main(arguments) == 1
    assert secrets_file.read_bytes() == original
    assert not secrets_file.with_suffix(".env.bak").exists()
    assert cli.main([*arguments, "--reset-existing"]) == 0
    assert secrets_file.read_bytes() != original
    assert secrets_file.with_suffix(".env.bak").read_bytes() == original
    assert request.admin_password not in capsys.readouterr().out


# 공백이 있는 영구 경로와 비기본 포트로 요청을 만들어 경로·입력 전달을 함께 검증한다.
@pytest.fixture
def request_files(tmp_path):
    server = tmp_path / "server"
    server.mkdir()
    (server / "compose.yml").write_text("services: {}", encoding="utf-8")
    identity = server / "runtime/models/osnet_x0_25_msmt17.onnx"
    identity.parent.mkdir(parents=True)
    identity.write_bytes(b"identity-model-presence-only")
    model = tmp_path / "model.pt"
    model.write_bytes(b"model-presence-only")
    certificate = tmp_path / "certificate.pem"
    certificate.write_text(
        "-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----\n",
        encoding="utf-8",
    )
    private_key = tmp_path / "private.pem"
    private_key.write_text(
        "-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    return InstallRequest(
        data_root=tmp_path / "installation with spaces",
        server_dir=server,
        admin_username="operator",
        admin_password="a-private-password",
        model_path=model,
        cameras=[CameraBootstrap(camera_id="cam-001", name="Entrance")],
        public_base_url="https://cctv.example.com:8443/",
        public_https_port=8443,
        rtsp_bind_address="0.0.0.0",
        rtsp_port=9554,
        tls_certificate_path=certificate,
        tls_private_key_path=private_key,
    )


# Docker와 실제 TLS 검증을 대체해 파일 생성·복원 계약을 설치 환경과 독립적으로 검사한다.
@pytest.fixture
def available_dependencies(monkeypatch):
    monkeypatch.setattr(
        workflow,
        "installation_prerequisites",
        lambda _: [Prerequisite(True, "Server package", "ok")],
    )
    # 테스트 PEM은 복사·입력 검사 전용이며 실제 TLS 파일인 것처럼 주장하지 않는다.
    monkeypatch.setattr(config_core, "_validate_certificate_key_match", lambda *_: None)


# 관리 화면 복원에 필요한 파일만 만들고 비밀 문자열을 심어 반환 정보의 노출을 검사한다.
def write_saved_installation(root: Path, server: Path):
    root.mkdir(parents=True, exist_ok=True)
    config = root / "config" / "config.yaml"
    write_config_atomic(
        AppConfig(
            server={
                "public_https_port": 8443,
                "rtsp_bind_address": "0.0.0.0",
                "rtsp_port": 9554,
            }
        ),
        config,
    )
    (root / "database").mkdir()
    (root / "secrets").mkdir()
    values = {
        "CONFIG_FILE": str(config.relative_to(server.parent)),
        "DATABASE_DIR": str((root / "database").relative_to(server.parent)),
        "PUBLIC_BASE_URL": "https://cctv.example.com:8443/",
        "PUBLIC_BIND_ADDRESS": "0.0.0.0",
    }
    for role in ("data", "external", "preprocessing", "media", "analysis"):
        path = root / "secrets" / f"{role}.env"
        path.write_text("PRIVATE_VALUE=do-not-display-this-token\n", encoding="utf-8")
        if role == "data":
            path.write_text(
                "INITIAL_ADMIN_USERNAME=operator\n"
                "INITIAL_ADMIN_PASSWORD_HASH=do-not-display-this-hash\n",
                encoding="utf-8",
            )
        values[f"{role.upper()}_SECRETS_FILE"] = str(path.relative_to(server.parent))
    # 상대 경로는 server_dir를 기준으로 해석해야 한다.
    env_file = root / "config" / "compose.env"
    for key in ("CONFIG_FILE", "DATABASE_DIR", *workflow._SECRET_KEYS):
        values[key] = "../" + values[key]
    env_file.write_text(
        "".join(f"{key}='{value}'\n" for key, value in values.items()),
        encoding="utf-8",
    )
    return env_file, config


# 모델·인증서만 미리 준비된 위치는 신규 설치로 분류하며 조회 자체는 파일을 만들지 않아야 한다.
def test_inspect_empty_or_staged_folder_is_new_without_writes(tmp_path):
    root = tmp_path / "installation"
    assert workflow.inspect_installation(root, tmp_path) is None
    assert not root.exists()
    root.mkdir()
    (root / "models").mkdir()
    (root / "certs").mkdir()
    assert workflow.inspect_installation(root, tmp_path) is None
    assert sorted(path.name for path in root.iterdir()) == ["certs", "models"]


# 상대 경로와 외부 접속 주소를 복원하면서 서비스 토큰은 Installation에 포함하지 않는지 확인한다.
def test_inspect_restores_paths_and_public_fields_without_service_secrets(tmp_path):
    server = tmp_path / "server"
    server.mkdir()
    root = tmp_path / "custom data"
    env, config = write_saved_installation(root, server)
    installation = workflow.inspect_installation(root, server)
    assert installation == workflow.Installation(
        root.resolve(),
        env,
        config,
        "https://cctv.example.com:8443",
        "cctv.example.com",
        9554,
        "operator",
    )
    assert "do-not-display" not in repr(asdict(installation))
    assert set(asdict(installation)) == {
        "data_root",
        "env_file",
        "config_file",
        "public_url",
        "rtsp_host",
        "rtsp_port",
        "admin_username",
    }


@pytest.mark.parametrize(
    "trace",
    ["config", "database", "secrets", "recordings", "cctv.db", workflow._MARKER],
)
def test_existing_traces_are_never_treated_as_new(tmp_path, trace):
    root = tmp_path / "data"
    root.mkdir()
    (root / trace).write_text("existing", encoding="utf-8")
    with pytest.raises(ValueError, match="복구"):
        workflow.inspect_installation(root, tmp_path)
    assert (root / trace).read_text(encoding="utf-8") == "existing"


# 손상된 YAML·누락된 인증 파일·중단 표식은 신규 설치 대신 비밀값 없는 복구 안내로 이어져야 한다.
@pytest.mark.parametrize(
    "damage", ["empty_yaml", "missing_secret", "invalid_yaml", "marker"]
)
def test_incomplete_installation_reports_recovery_without_input_values(
    tmp_path, damage
):
    server = tmp_path / "server"
    server.mkdir()
    root = tmp_path / "data"
    _, config = write_saved_installation(root, server)
    if damage == "empty_yaml":
        config.write_text("{}", encoding="utf-8")
    elif damage == "missing_secret":
        (root / "secrets" / "analysis.env").unlink()
    elif damage == "invalid_yaml":
        config.write_text("server: do-not-display-this-secret", encoding="utf-8")
    else:
        (root / workflow._MARKER).touch()
    with pytest.raises(ValueError, match="복구") as error:
        workflow.inspect_installation(root, server)
    assert "do-not-display" not in str(error.value)
    assert error.value.__suppress_context__


def test_preflight_reports_all_file_failures_even_without_docker(tmp_path, monkeypatch):
    monkeypatch.setattr(
        workflow,
        "installation_prerequisites",
        lambda _: [Prerequisite(False, "Docker Desktop", "private engine output")],
    )
    results = workflow.preflight(tmp_path, tmp_path / "missing.pt", None, None)
    assert len(results) == 4
    assert all(not item.ok for item in results)
    assert "private engine output" not in repr(results)
    assert any("모델" in item.name for item in results)
    assert any("인증서" in item.name for item in results)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    "os_type,expected", [("linux", True), ("windows", False), ("", False)]
)
def test_preflight_checks_docker_linux_mode(
    request_files, available_dependencies, monkeypatch, os_type, expected
):
    monkeypatch.setattr(
        workflow,
        "installation_prerequisites",
        lambda _: [Prerequisite(True, "Docker Engine", "running")],
    )
    monkeypatch.setattr(workflow.shutil, "which", lambda _: "docker")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout=os_type)

    monkeypatch.setattr(workflow.subprocess, "run", run)
    result = workflow.preflight(
        request_files.server_dir,
        request_files.model_path,
        request_files.tls_certificate_path,
        request_files.tls_private_key_path,
    )
    assert (
        next(item.ok for item in result if item.name == "Linux 컨테이너 모드")
        is expected
    )
    assert calls[0][0] == ["docker", "info", "--format", "{{.OSType}}"]
    assert calls[0][1]["timeout"] == 20


# 이 경우에는 TLS 검증을 대체하지 않아 PEM 헤더만 맞는 가짜 인증서를 실제로 거부하는지 확인한다.
def test_preflight_rejects_malformed_certificate_with_real_tls_check(
    request_files, monkeypatch
):
    monkeypatch.setattr(workflow, "installation_prerequisites", lambda _: [])
    result = workflow.preflight(
        request_files.server_dir,
        request_files.model_path,
        request_files.tls_certificate_path,
        request_files.tls_private_key_path,
    )
    assert result[-1].ok is False
    assert "일치하지" in result[-1].message
    assert not request_files.data_root.exists()


# 암호·포트·URL·중복 카메라·TLS 오류는 초기화 호출과 디렉터리 생성 전에 검출되어야 한다.
@pytest.mark.parametrize(
    "change", ["password", "ports", "url", "duplicate_camera", "tls"]
)
def test_invalid_inputs_never_create_installation_files(
    request_files, available_dependencies, monkeypatch, change
):
    changes = {
        "password": {"admin_password": "short"},
        "ports": {"rtsp_port": request_files.public_https_port},
        "url": {"public_base_url": "https://user:do-not-display-password@example.com"},
        "duplicate_camera": {"cameras": request_files.cameras * 2},
        "tls": {"tls_private_key_path": None},
    }
    monkeypatch.setattr(
        workflow, "initialize", lambda _: pytest.fail("must not initialize")
    )
    with pytest.raises(ValueError) as error:
        workflow.install_new(replace(request_files, **changes[change]))
    assert "do-not-display-password" not in str(error.value)
    assert not request_files.data_root.exists()


def test_existing_installation_is_blocked_before_prerequisite_checks(
    request_files, monkeypatch
):
    existing = request_files.data_root / "database"
    existing.mkdir(parents=True)
    database = existing / "cctv.db"
    database.write_bytes(b"preserved database")
    monkeypatch.setattr(
        workflow, "preflight", lambda *_: pytest.fail("must stop before checks")
    )
    with pytest.raises(ValueError, match="복구"):
        workflow.install_new(request_files)
    assert database.read_bytes() == b"preserved database"


def test_existing_custom_env_is_not_overwritten(request_files, monkeypatch, tmp_path):
    env = tmp_path / "custom.env"
    env.write_text("PRIVATE_TOKEN=preserve", encoding="utf-8")
    monkeypatch.setattr(
        workflow, "preflight", lambda *_: pytest.fail("must stop before checks")
    )
    with pytest.raises(ValueError, match="복구"):
        workflow.install_new(replace(request_files, compose_env_path=env))
    assert env.read_text(encoding="utf-8") == "PRIVATE_TOKEN=preserve"


# 사전 검사 도중 다른 설치의 파일이 생기는 경쟁 상황을 재현해 두 번째 존재 검사를 확인한다.
def test_installation_created_during_checks_is_preserved(
    request_files, available_dependencies, monkeypatch
):
    def preflight(*_):
        (request_files.data_root / "database").mkdir(parents=True)
        return [Prerequisite(True, "ready", "ready")]

    monkeypatch.setattr(workflow, "preflight", preflight)
    monkeypatch.setattr(
        workflow, "initialize", lambda _: pytest.fail("must not initialize")
    )
    with pytest.raises(ValueError, match="복구"):
        workflow.install_new(request_files)
    assert (request_files.data_root / "database").is_dir()
    assert not (request_files.data_root / workflow._MARKER).exists()


# 일부 파일 생성 후 실패하면 표식을 유지하고 재실행을 막아 인증키와 기존 상태가 덮이지 않게 한다.
def test_failure_retains_marker_and_blocks_retry_without_exposing_exception(
    request_files, available_dependencies, monkeypatch
):
    calls = []

    def initialize(request):
        calls.append(request)
        (request.data_root / "config").mkdir()
        raise OSError("a-private-password private-token-output")

    monkeypatch.setattr(workflow, "initialize", initialize)
    with pytest.raises(RuntimeError, match="복구") as error:
        workflow.install_new(request_files)
    assert "private" not in str(error.value)
    assert error.value.__suppress_context__
    assert (request_files.data_root / workflow._MARKER).is_file()
    with pytest.raises(ValueError, match="복구"):
        workflow.install_new(request_files)
    assert len(calls) == 1


def test_success_uses_persistent_env_restores_installation_and_blocks_reinitialization(
    request_files, available_dependencies
):
    # 실제 initialize를 호출하여 config·env·토큰을 작성하고 복원까지 연결한다.
    result = workflow.install_new(request_files)
    assert result.compose_env_path == request_files.data_root / "config" / "compose.env"
    assert not (request_files.server_dir / ".env").exists()
    assert not (request_files.data_root / workflow._MARKER).exists()
    restored = workflow.inspect_installation(
        request_files.data_root, request_files.server_dir
    )
    assert restored.admin_username == "operator"
    assert restored.public_url == "https://cctv.example.com:8443"
    assert restored.rtsp_host == "cctv.example.com"
    before = read_deployment_env(result.secrets_path)
    with pytest.raises(ValueError, match="복구"):
        workflow.install_new(request_files)
    assert read_deployment_env(result.secrets_path) == before
    assert request_files.admin_password not in repr(restored)
