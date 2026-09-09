# CLI·Compose·관리자 API와 수동 운영 도구의 설정·인증 계약을 실제 서버 없이 검증한다.
import io
import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler

import pytest

from server.setup.install_helper import cli as install_helper_cli
from server.setup.install_helper import compose_adapter
from server.setup.install_helper.cli import build_parser
from server.setup.install_helper.compose_adapter import (
    ComposeAdapter,
    default_compose_env,
    default_server_dir,
)
from server.setup.install_helper.server_api import (
    ServerApiClient,
    ServerApiError,
    _NoRedirectHandler,
    _validate_edge_url,
    redact_for_display,
)
from server.services.data.tools import backup_database
from server.services.external.tools import bootstrap_admin
from server.setup.install_helper import doctor as server_doctor
from server.setup.tools import generate_secrets
from server.setup.config_core import InstallRequest


def _env_value(text: str, key: str) -> str:
    return next(
        line.partition("=")[2].strip("'")
        for line in text.splitlines()
        if line.startswith(f"{key}=")
    )


# 컨테이너 내부 운영 도구가 필요한 역할 토큰을 사용하고 환경 프록시로 인증값을 보내지 않게 한다.
def test_internal_operator_scripts_pin_scoped_token_and_ignore_proxy_env():
    assert 'os.environ["DATA_EXTERNAL_TOKEN"]' in bootstrap_admin.CONTAINER_SCRIPT
    assert "trust_env=False" in bootstrap_admin.CONTAINER_SCRIPT
    assert 'os.environ["DATA_EXTERNAL_TOKEN"]' in backup_database.CONTAINER_SCRIPT
    assert "ProxyHandler({})" in backup_database.CONTAINER_SCRIPT
    assert "NoRedirectHandler" in backup_database.CONTAINER_SCRIPT
    assert "INTERNAL_SERVICE_TOKEN" not in bootstrap_admin.CONTAINER_SCRIPT
    assert "INTERNAL_SERVICE_TOKEN" not in backup_database.CONTAINER_SCRIPT


# 수동 생성한 인증 파일의 역할 분리·토큰 일치·출력 비노출이 Compose 계약과 맞는지 확인한다.
def test_manual_secret_generator_splits_service_privileges(
    tmp_path, monkeypatch, capsys
):
    output_dir = tmp_path / "secrets"
    restricted: list[str] = []

    def record_private_file(path: Path) -> None:
        os.chmod(path, 0o600)
        restricted.append(path.name)

    monkeypatch.setattr(generate_secrets, "restrict_private_file", record_private_file)
    monkeypatch.setattr(
        "sys.argv",
        [
            "generate_secrets.py",
            "--output-dir",
            str(output_dir),
            "--camera-id",
            "cam-001",
        ],
    )

    assert generate_secrets.main() == 0

    data = (output_dir / "data.env").read_text(encoding="utf-8")
    external = (output_dir / "external.env").read_text(encoding="utf-8")
    inference = (output_dir / "preprocessing.env").read_text(encoding="utf-8")
    analysis = (output_dir / "analysis.env").read_text(encoding="utf-8")
    media = (output_dir / "media.env").read_text(encoding="utf-8")
    scoped_tokens = {
        key: _env_value(data, key)
        for key in (
            "DATA_EXTERNAL_TOKEN",
            "DATA_INFERENCE_TOKEN",
            "DATA_MEDIA_TOKEN",
            "DATA_RECOVERY_TOKEN",
            "DATA_IDENTITY_TOKEN",
            "DATA_ANALYSIS_TOKEN",
        )
    }
    assert len(set(scoped_tokens.values())) == 6
    assert (
        _env_value(inference, "DATA_IDENTITY_TOKEN")
        == scoped_tokens["DATA_IDENTITY_TOKEN"]
    )
    assert (
        _env_value(analysis, "DATA_ANALYSIS_TOKEN")
        == scoped_tokens["DATA_ANALYSIS_TOKEN"]
    )
    assert (
        _env_value(external, "DATA_EXTERNAL_TOKEN")
        == scoped_tokens["DATA_EXTERNAL_TOKEN"]
    )
    assert (
        _env_value(inference, "DATA_INFERENCE_TOKEN")
        == scoped_tokens["DATA_INFERENCE_TOKEN"]
    )
    assert _env_value(media, "DATA_MEDIA_TOKEN") == scoped_tokens["DATA_MEDIA_TOKEN"]
    assert "JWT_SECRET=" not in data
    assert "JWT_SECRET=" in external
    assert "MEDIA_PUBLISH_CREDENTIALS_JSON=" not in inference
    assert "MEDIA_PUBLISH_CREDENTIALS_JSON=" not in media
    assert _env_value(external, "MEDIA_READ_USERNAME") == _env_value(
        inference, "MEDIA_READ_USERNAME"
    )
    assert _env_value(external, "MEDIA_READ_PASSWORD") == _env_value(
        inference, "MEDIA_READ_PASSWORD"
    )
    assert len(_env_value(external, "MEDIA_READ_PASSWORD")) >= 32
    assert "MEDIA_READ_USERNAME=" not in data
    assert "MEDIA_READ_PASSWORD=" not in data
    assert "MEDIA_READ_USERNAME=" not in media
    assert "MEDIA_READ_PASSWORD=" not in media
    output = capsys.readouterr().out
    assert "bootstrap-only publish credentials" in output
    assert all(token not in output for token in scoped_tokens.values())
    assert len(restricted) == 5

    compose = Path("server/compose.yml").read_text(encoding="utf-8")
    assert "${DATA_SECRETS_FILE:-./secrets/data.env}" in compose
    assert "${EXTERNAL_SECRETS_FILE:-./secrets/external.env}" in compose
    assert "${PREPROCESSING_SECRETS_FILE:-./secrets/preprocessing.env}" in compose
    assert "${MEDIA_SECRETS_FILE:-./secrets/media.env}" in compose
    assert "${SECRETS_FILE" not in compose
    assert "INTERNAL_CLIENT_SECRETS_FILE" not in compose
    assert (
        "CENTRAL_RECORDING_SEGMENT_SECONDS: ${RECORDING_SEGMENT_SECONDS:-60}" in compose
    )
    assert (
        "MTX_PATHDEFAULTS_RECORDSEGMENTDURATION: "
        "${RECORDING_SEGMENT_SECONDS:-60}s" in compose
    )

    env_example = Path("server/.env.example").read_text(encoding="utf-8")
    assert "AI_CCTV_VERSION=0.3.0\n" in env_example
    assert "RECORDING_SEGMENT_SECONDS=60\n" in env_example


# doctor가 검사할 파일 구조와 실제 생성된 서비스 비밀 파일을 준비하며 파일 권한 호출은 대체한다.
def _prepare_doctor_deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    server_dir = tmp_path / "server"
    for directory in (
        server_dir / "config",
        server_dir / "services" / "nginx",
        server_dir / "services" / "mediamtx",
        server_dir / "runtime" / "certificates",
        server_dir / "runtime" / "database",
        server_dir / "runtime" / "recordings",
        server_dir / "runtime" / "recovered",
        server_dir / "runtime" / "snapshots",
        server_dir / "runtime" / "models",
        server_dir / "runtime" / "logs",
    ):
        directory.mkdir(parents=True, exist_ok=True)
    for path in (
        server_dir / "compose.yml",
        server_dir / "config" / "config.yaml",
        server_dir / "services" / "nginx" / "nginx.conf",
        server_dir / "services" / "mediamtx" / "mediamtx.yml",
        server_dir / "runtime" / "certificates" / "tls.crt",
        server_dir / "runtime" / "certificates" / "tls.key",
    ):
        path.write_text("test\n", encoding="utf-8")
    (server_dir / ".env").write_text(
        "DATA_SECRETS_FILE=./secrets/data.env\n"
        "EXTERNAL_SECRETS_FILE=./secrets/external.env\n"
        "PREPROCESSING_SECRETS_FILE=./secrets/preprocessing.env\n"
        "MEDIA_SECRETS_FILE=./secrets/media.env\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        generate_secrets,
        "restrict_private_file",
        lambda path: os.chmod(path, 0o600),
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "generate_secrets.py",
            "--output-dir",
            str(server_dir / "secrets"),
        ],
    )
    assert generate_secrets.main() == 0
    return server_dir


# 정상 배포에서 토큰·허용 키·미디어 계정을 차례로 바꿔 doctor가 각 불일치를 보고하는지 확인한다.
def test_server_doctor_validates_scoped_tokens_and_secret_allowlists(
    tmp_path, monkeypatch, capsys
):
    server_dir = _prepare_doctor_deployment(tmp_path, monkeypatch)
    capsys.readouterr()
    monkeypatch.setattr(
        "sys.argv",
        [
            "doctor.py",
            "--server-dir",
            str(server_dir),
            "--skip-runtime",
            "--skip-compose",
        ],
    )
    assert server_doctor.main() == 0

    data_path = server_dir / "secrets" / "data.env"
    data_path.write_text(
        data_path.read_text(encoding="utf-8")
        + f'EDGE_AUTH_TOKENS_JSON=\'{{"edge-001":"{"e" * 40}"}}\'\n',
        encoding="utf-8",
    )
    assert server_doctor.main() == 0

    inference_path = server_dir / "secrets" / "preprocessing.env"
    original = inference_path.read_text(encoding="utf-8")
    inference_path.write_text(
        original.replace(_env_value(original, "DATA_INFERENCE_TOKEN"), "m" * 40),
        encoding="utf-8",
    )
    assert server_doctor.main() == 1
    assert "matches between data.env and preprocessing.env" in capsys.readouterr().out

    inference_path.write_text(
        original + f"DATA_EXTERNAL_TOKEN={'x' * 40}\n", encoding="utf-8"
    )
    assert server_doctor.main() == 1
    assert "forbidden: DATA_EXTERNAL_TOKEN" in capsys.readouterr().out

    inference_path.write_text(
        original.replace(
            _env_value(original, "MEDIA_READ_PASSWORD"), "different-" + "p" * 40
        ),
        encoding="utf-8",
    )
    assert server_doctor.main() == 1
    assert (
        "MEDIA_READ_PASSWORD matches between external.env and preprocessing.env"
        in capsys.readouterr().out
    )

    inference_path.write_text(
        original.replace(
            _env_value(original, "MEDIA_READ_USERNAME"), "other-inference-reader"
        ),
        encoding="utf-8",
    )
    assert server_doctor.main() == 1
    assert (
        "MEDIA_READ_USERNAME matches between external.env and preprocessing.env"
        in capsys.readouterr().out
    )

    inference_path.write_text(
        original.replace(_env_value(original, "MEDIA_READ_PASSWORD"), "too-short"),
        encoding="utf-8",
    )
    assert server_doctor.main() == 1
    assert (
        "preprocessing.env contains a 32+ character MEDIA_READ_PASSWORD"
        in capsys.readouterr().out
    )


def test_install_helper_rtsp_bind_defaults_to_loopback_and_requires_explicit_lan_ip():
    assert InstallRequest.__dataclass_fields__["rtsp_bind_address"].default == (
        "127.0.0.1"
    )
    args = build_parser().parse_args(
        [
            "init",
            "--data-root",
            "runtime",
            "--model",
            "model.pt",
            "--public-base-url",
            "https://cctv.example.com",
        ]
    )
    assert args.rtsp_bind == "127.0.0.1"
    assert args.recording_segment_seconds == 60
    assert args.retention_days == 7
    assert args.storage_warning_free_percent == 15
    assert args.inference_device == "auto"
    help_text = (
        build_parser().format_help()
        + build_parser()._subparsers._group_actions[0].choices["init"].format_help()
    )
    assert "trusted-LAN IP" in help_text
    assert "recording segment length" in help_text


def test_server_doctor_rejects_legacy_combined_secret_deployment(
    tmp_path, monkeypatch, capsys
):
    server_dir = _prepare_doctor_deployment(tmp_path, monkeypatch)
    (server_dir / ".env").write_text(
        "SECRETS_FILE=./secrets/secrets.env\n", encoding="utf-8"
    )
    capsys.readouterr()
    monkeypatch.setattr(
        "sys.argv",
        [
            "doctor.py",
            "--server-dir",
            str(server_dir),
            "--skip-runtime",
            "--skip-compose",
        ],
    )
    assert server_doctor.main() == 1
    output = capsys.readouterr().out
    assert "migrate SECRETS_FILE/INTERNAL_CLIENT_SECRETS_FILE" in output


def test_frozen_install_helper_discovers_programdata_compose_env(tmp_path, monkeypatch):
    program_data = tmp_path / "ProgramData"
    expected = program_data / "AI_CCTV" / "config" / "compose.env"
    expected.parent.mkdir(parents=True)
    expected.write_text("TEST=1\n", encoding="utf-8")
    monkeypatch.setenv("PROGRAMDATA", str(program_data))
    monkeypatch.delenv("AI_CCTV_COMPOSE_ENV_FILE", raising=False)
    monkeypatch.setattr(compose_adapter.sys, "frozen", True, raising=False)

    assert default_compose_env(tmp_path / "Program Files" / "AI_CCTV") == (
        expected.resolve()
    )


def test_default_server_dir_follows_source_and_installed_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(compose_adapter.sys, "frozen", False, raising=False)
    repository_root = Path(__file__).resolve().parents[4]
    assert default_server_dir() == repository_root / "server"

    executable = tmp_path / "AI_CCTV" / "AI_CCTV_Install_Helper.exe"
    monkeypatch.setattr(compose_adapter.sys, "frozen", True, raising=False)
    monkeypatch.setattr(compose_adapter.sys, "executable", str(executable))
    assert default_server_dir() == executable.parent / "server"


def test_compose_adapter_keeps_explicit_env_outside_server_package(tmp_path):
    server_dir = tmp_path / "server-package"
    compose_env = tmp_path / "program-data" / "config" / "compose.env"
    compose_env.parent.mkdir(parents=True)
    compose_env.write_text("TEST=1\n", encoding="utf-8")

    assert ComposeAdapter(server_dir, compose_env).env_file == compose_env.resolve()


def test_compose_env_double_quotes_match_shared_parser_and_activate_push(
    tmp_path, monkeypatch
):
    from server.setup.install_helper import compose_adapter
    from server.setup.validation import read_deployment_env

    env = tmp_path / "compose.env"
    env.write_text('CONFIG_FILE="./config with spaces/config.yaml"\n', encoding="utf-8")
    push = tmp_path / "push.env"
    push.write_text('PUSH_ENABLED="true"\n', encoding="utf-8")
    assert compose_adapter._env_values(env) == read_deployment_env(env)
    adapter = ComposeAdapter(tmp_path, env)
    assert str(tmp_path / "compose.push.yml") in adapter.command("up")
    monkeypatch.setattr(compose_adapter, "installation_prerequisites", lambda _: [])
    config = tmp_path / "config with spaces" / "config.yaml"
    config.parent.mkdir()
    config.write_text("schema_version: 1", encoding="utf-8")
    checks = adapter.deployment_prerequisites()
    assert next(item for item in checks if item.name == "Configuration").ok


def test_cli_exposes_installation_and_host_controls_only():
    commands = set(build_parser()._subparsers._group_actions[0].choices)
    assert {
        "init",
        "install",
        "preflight",
        "validate",
        "doctor",
        "start",
        "stop",
        "restart",
        "status",
        "logs",
        "edge-register",
    } <= commands
    assert commands.isdisjoint(
        {
            "edge-update",
            "edge-rotate-credentials",
            "camera-status",
            "video-profile",
            "set-video-profile",
        }
    )


def test_cli_init_reports_input_error_without_traceback(tmp_path, capsys):
    password = tmp_path / "admin-password.txt"
    password.write_text("a-strong-password", encoding="utf-8")
    result = install_helper_cli.main(
        [
            "--server-dir",
            str(tmp_path / "server"),
            "init",
            "--data-root",
            str(tmp_path / "data"),
            "--admin-password-file",
            str(password),
            "--model",
            str(tmp_path / "missing.pt"),
        ]
    )

    assert result == 1
    output = capsys.readouterr().out
    assert "[ERROR] INITIALIZATION_FAILED" in output
    assert "Traceback" not in output


def test_frozen_cli_exposes_consumer_local_model_flow():
    init_help = (
        build_parser()._subparsers._group_actions[0].choices["init"].format_help()
    )
    assert "--model MODEL" in init_help
    assert "--model-manifest" not in init_help
    assert build_parser().parse_args(
        ["--env-file", "before.env", "stop"]
    ).env_file == Path("before.env")
    assert build_parser().parse_args(
        ["stop", "--env-file", "after.env"]
    ).env_file == Path("after.env")


class _JsonResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


# 준비한 JSON 응답이나 HTTP 오류를 순서대로 반환하고 요청·제한 시간을 기록한다.
class _QueuedOpener:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, *, timeout):
        self.requests.append((request, timeout))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return _JsonResponse(json.dumps(response).encode("utf-8"))


# 로그인은 공개 요청, 등록은 Bearer 요청으로 보내며 URL에 토큰이 포함되지 않아야 한다.
def test_server_api_registers_edge_without_query_tokens():
    opener = _QueuedOpener(
        {
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
            "token_type": "bearer",
        },
        {"camera_id": "cam-001", "edge_device_id": "edge-001"},
    )
    client = ServerApiClient("https://cctv.example.com/", opener=opener)

    login_result = client.login("admin", "administrator-password")
    assert login_result["access_token"] == "[redacted]"
    client.register_edge(
        camera_id="cam-001",
        name="Entrance",
        edge_device_id="edge-001",
        edge_management_url="http://192.0.2.41:8003",
        edge_recovery_url="http://192.0.2.41:8002",
        edge_auth_token="e" * 32,
    )

    login_request = opener.requests[0][0]
    assert login_request.full_url == "https://cctv.example.com/api/v1/auth/login"
    assert login_request.get_header("Authorization") is None

    register_request = opener.requests[1][0]
    assert register_request.get_header("Authorization") == "Bearer access-secret"
    assert json.loads(register_request.data) == {
        "camera_id": "cam-001",
        "name": "Entrance",
        "edge_device_id": "edge-001",
        "edge_management_url": "http://192.0.2.41:8003",
        "edge_recovery_url": "http://192.0.2.41:8002",
        "edge_auth_token": "e" * 32,
        "enabled": True,
    }
    assert register_request.full_url == "https://cctv.example.com/api/v1/cameras"
    assert register_request.method == "POST"
    assert "access-secret" not in register_request.full_url
    assert opener.requests[1][1] == 90.0


def test_server_api_registration_error_is_operator_facing():
    failure_body = json.dumps(
        {
            "error": {
                "code": "CAMERA_ALREADY_EXISTS",
                "message": "This camera is already registered.",
                "details": {"camera_id": "cam-001"},
            }
        }
    ).encode("utf-8")
    failure = HTTPError(
        "https://cctv.example.com/api/v1/cameras",
        409,
        "Conflict",
        {},
        io.BytesIO(failure_body),
    )
    opener = _QueuedOpener(
        {"access_token": "access-secret"},
        failure,
    )
    client = ServerApiClient("https://cctv.example.com", opener=opener)
    client.login("admin", "administrator-password")
    with pytest.raises(ServerApiError) as captured:
        client.register_edge(
            camera_id="cam-001",
            name="Entrance",
            edge_device_id="edge-001",
            edge_management_url="http://192.0.2.41:8003",
            edge_recovery_url="http://192.0.2.41:8002",
            edge_auth_token="e" * 32,
        )
    assert captured.value.status_code == 409
    assert captured.value.code == "CAMERA_ALREADY_EXISTS"
    assert "already registered" in captured.value.message
    assert captured.value.details == {"camera_id": "cam-001"}


# 중첩 자격 증명을 가리고 비로컬 HTTP·URL 내 인증값·잘못된 경로·리다이렉트를 차단하는지 확인한다.
def test_install_helper_redacts_nested_credentials_and_rejects_unsafe_server_url():
    assert redact_for_display(
        {
            "camera_id": "cam-001",
            "publish_credentials": {"username": "cam-001", "password": "secret"},
            "edge_auth_token": "edge-secret",
        }
    ) == {
        "camera_id": "cam-001",
        "publish_credentials": "[redacted]",
        "edge_auth_token": "[redacted]",
    }
    with pytest.raises(ValueError, match="must not contain credentials"):
        ServerApiClient("https://admin:secret@cctv.example.com")
    with pytest.raises(ValueError, match="must not contain a path"):
        ServerApiClient("https://cctv.example.com/api")
    with pytest.raises(ValueError, match="must use HTTPS"):
        ServerApiClient("http://cctv.example.com")
    assert ServerApiClient("http://127.0.0.1").base_url == "http://127.0.0.1"
    assert ServerApiClient("http://localhost:8443").base_url.endswith(":8443")
    assert (
        _NoRedirectHandler().redirect_request(
            None, None, 302, "Found", {}, "https://other.example.com"
        )
        is None
    )
    with pytest.raises(ValueError, match="invalid path"):
        _validate_edge_url("https://edge.example.com/../admin", "Edge management URL")


def test_server_api_client_default_opener_ignores_environment_proxy(monkeypatch):
    captured = []

    class Opener:
        def open(self, *_args, **_kwargs):
            raise AssertionError("network should not be called")

    def fake_build_opener(*handlers):
        captured.extend(handlers)
        return Opener()

    monkeypatch.setattr(
        "server.setup.install_helper.server_api.build_opener", fake_build_opener
    )
    ServerApiClient("https://cctv.example.com")

    proxy_handlers = [item for item in captured if isinstance(item, ProxyHandler)]
    assert len(proxy_handlers) == 1
    assert proxy_handlers[0].proxies == {}


# CLI가 관리·복구 주소를 구분해 등록하고 게시 계정을 보호 파일에만 쓰는 전체 흐름을 검사한다.
def test_edge_register_cli_uses_separate_urls_and_never_prints_secrets(
    tmp_path, monkeypatch, capsys
):
    password_file = tmp_path / "admin-password.txt"
    password_file.write_text("administrator-password", encoding="utf-8")
    token_file = tmp_path / "edge-token.txt"
    token_file.write_text("e" * 32, encoding="utf-8")
    handoff_file = tmp_path / "handoff" / "cam-001-publish.json"
    captured = {}
    protected_paths = []

    class FakeClient:
        def __init__(self, base_url):
            captured["base_url"] = base_url

        def login(self, username, password):
            captured["login"] = (username, password)
            return {"access_token": "[redacted]"}

        def register_edge(self, **payload):
            captured["payload"] = payload
            return {
                "camera_id": payload["camera_id"],
                "publish_credentials": {
                    "username": payload["camera_id"],
                    "password": "publish-secret",
                },
                "edge_auth_token": payload["edge_auth_token"],
            }

    monkeypatch.setattr(install_helper_cli, "ServerApiClient", FakeClient)
    monkeypatch.setattr(
        "server.setup.install_helper.server_api.restrict_private_file",
        lambda path: protected_paths.append(path),
    )
    result = install_helper_cli.main(
        [
            "edge-register",
            "cam-001",
            "--server-url",
            "https://cctv.example.com",
            "--username",
            "admin",
            "--password-file",
            str(password_file),
            "--name",
            "Entrance",
            "--edge-device-id",
            "edge-001",
            "--management-url",
            "http://192.0.2.41:8003",
            "--recovery-url",
            "http://192.0.2.41:8002",
            "--edge-auth-token-file",
            str(token_file),
            "--publish-credentials-output",
            str(handoff_file),
        ]
    )

    assert result == 0
    assert captured["payload"]["edge_management_url"].endswith(":8003")
    assert captured["payload"]["edge_recovery_url"].endswith(":8002")
    assert json.loads(handoff_file.read_text(encoding="utf-8")) == {
        "camera_id": "cam-001",
        "username": "cam-001",
        "password": "publish-secret",
    }
    assert protected_paths
    output = capsys.readouterr().out
    assert "publish-secret" not in output
    assert "e" * 32 not in output
    assert output.count("[redacted]") == 2
