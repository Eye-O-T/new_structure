import io
import json
import os
import stat
import subprocess
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import ProxyHandler

import pytest

from ai_cctv_core.config import CameraBootstrap, load_config
from configurator import cli as configurator_cli
from configurator import compose_adapter
from configurator.cli import build_parser
from configurator.compose_adapter import ComposeAdapter, default_compose_env
from configurator.config_core import (
    InstallRequest,
    _dotenv,
    _validate_public_base_url,
    initialize,
)
from configurator.gui import rotate_publish_credentials_to_file
from configurator.model_manager import install_local_model
from configurator.server_api import (
    ServerApiClient,
    ServerApiError,
    _NoRedirectHandler,
    _validate_edge_url,
    redact_for_display,
)
from server.scripts import backup_database, bootstrap_admin
from server.scripts import doctor as server_doctor
from server.scripts import generate_secrets


def _env_value(text: str, key: str) -> str:
    return next(
        line.partition("=")[2].strip("'")
        for line in text.splitlines()
        if line.startswith(f"{key}=")
    )


def test_server_installer_excludes_generated_secret_files():
    installer = Path("configurator/packaging/AI_CCTV_Server.iss").read_text(
        encoding="utf-8"
    )
    server_source = next(
        line
        for line in installer.splitlines()
        if line.startswith('Source: "..\\..\\server\\*"')
    )
    assert "secrets\\*.env" in server_source
    assert "secrets\\*.json" in server_source
    assert "runtime\\*" in server_source
    ignored = Path(".gitignore").read_text(encoding="utf-8").splitlines()
    assert "server/secrets/*.json" in ignored


def test_internal_operator_scripts_pin_scoped_token_and_ignore_proxy_env():
    assert 'os.environ["DATA_EXTERNAL_TOKEN"]' in bootstrap_admin.CONTAINER_SCRIPT
    assert "trust_env=False" in bootstrap_admin.CONTAINER_SCRIPT
    assert 'os.environ["DATA_EXTERNAL_TOKEN"]' in backup_database.CONTAINER_SCRIPT
    assert "ProxyHandler({})" in backup_database.CONTAINER_SCRIPT
    assert "NoRedirectHandler" in backup_database.CONTAINER_SCRIPT
    assert "INTERNAL_SERVICE_TOKEN" not in bootstrap_admin.CONTAINER_SCRIPT
    assert "INTERNAL_SERVICE_TOKEN" not in backup_database.CONTAINER_SCRIPT


def test_initialize_generates_valid_config_and_non_plaintext_secrets(tmp_path):
    server_dir = tmp_path / "server"
    server_dir.mkdir()
    model_source = tmp_path / "selected-model.pt"
    model_source.write_bytes(b"model-content")
    result = initialize(
        InstallRequest(
            data_root=tmp_path / "data",
            server_dir=server_dir,
            admin_username="admin",
            admin_password="a-strong-password",
            model_path=model_source,
            cameras=[CameraBootstrap(camera_id="cam-001", name="Entrance")],
            public_base_url="https://cctv.example.com/",
        )
    )
    config = load_config(result.config_path)
    assert config.cameras[0].stream_path == "cam-001"
    assert config.inference.model_path == "/models/selected-model.pt"
    assert (tmp_path / "data" / "models" / "selected-model.pt").read_bytes() == (
        b"model-content"
    )

    data_secrets = result.secrets_path.read_text(encoding="utf-8")
    external_secrets = result.external_secrets_path.read_text(encoding="utf-8")
    inference_secrets = result.inference_secrets_path.read_text(encoding="utf-8")
    media_secrets = result.media_secrets_path.read_text(encoding="utf-8")
    assert "a-strong-password" not in data_secrets
    assert "INITIAL_ADMIN_PASSWORD_HASH='$argon2" in data_secrets
    assert "JWT_SECRET=" not in data_secrets
    assert "MEDIA_PUBLISH_CREDENTIALS_JSON=" not in data_secrets
    assert "MEDIA_READ_USERNAME=" not in data_secrets
    assert "MEDIA_READ_PASSWORD=" not in data_secrets
    assert "JWT_SECRET=" in external_secrets
    assert "MEDIA_PUBLISH_CREDENTIALS_JSON=" in external_secrets
    assert "INITIAL_ADMIN_PASSWORD_HASH=" not in external_secrets
    assert inference_secrets.startswith("DATA_INFERENCE_TOKEN=")
    assert media_secrets.startswith("DATA_MEDIA_TOKEN=")
    assert "JWT_SECRET=" not in inference_secrets
    assert "JWT_SECRET=" not in media_secrets
    assert "MEDIA_READ_USERNAME=" not in media_secrets
    assert "MEDIA_READ_PASSWORD=" not in media_secrets
    assert _env_value(external_secrets, "MEDIA_READ_USERNAME") == _env_value(
        inference_secrets, "MEDIA_READ_USERNAME"
    )
    assert _env_value(external_secrets, "MEDIA_READ_PASSWORD") == _env_value(
        inference_secrets, "MEDIA_READ_PASSWORD"
    )
    assert len(_env_value(inference_secrets, "MEDIA_READ_PASSWORD")) >= 32
    scoped_tokens = {
        key: _env_value(data_secrets, key)
        for key in (
            "DATA_EXTERNAL_TOKEN",
            "DATA_INFERENCE_TOKEN",
            "DATA_MEDIA_TOKEN",
            "DATA_RECOVERY_TOKEN",
        )
    }
    assert len(set(scoped_tokens.values())) == 4
    assert (
        _env_value(external_secrets, "DATA_EXTERNAL_TOKEN")
        == scoped_tokens["DATA_EXTERNAL_TOKEN"]
    )
    assert (
        _env_value(inference_secrets, "DATA_INFERENCE_TOKEN")
        == scoped_tokens["DATA_INFERENCE_TOKEN"]
    )
    assert (
        _env_value(media_secrets, "DATA_MEDIA_TOKEN")
        == scoped_tokens["DATA_MEDIA_TOKEN"]
    )
    assert "INTERNAL_SERVICE_TOKEN=" not in data_secrets
    # NTFS does not expose its ACL as POSIX permission bits: Python reports 0666
    # even after chmod(0600).  POSIX hosts can and must verify the actual mode.
    if os.name != "nt":
        assert stat.S_IMODE(result.secrets_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(result.external_secrets_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(result.inference_secrets_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(result.media_secrets_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(result.camera_credentials_path.stat().st_mode) == 0o600
    assert result.camera_credentials_path.read_text(encoding="utf-8").startswith("{")
    assert (
        result.camera_credentials["cam-001"]["password"] not in config.model_dump_json()
    )

    compose_text = result.compose_env_path.read_text(encoding="utf-8")
    assert "MODEL_FILE=selected-model.pt\n" in compose_text
    assert "RTSP_BIND_ADDRESS=127.0.0.1\n" in compose_text
    assert "PUBLIC_BASE_URL=https://cctv.example.com\n" in compose_text
    assert "DATA_SECRETS_FILE=" in compose_text
    assert "EXTERNAL_SECRETS_FILE=" in compose_text
    assert "INFERENCE_SECRETS_FILE=" in compose_text
    assert "MEDIA_SECRETS_FILE=" in compose_text
    assert "\nSECRETS_FILE=" not in compose_text
    expected_models_dir = _dotenv(str(tmp_path / "data" / "models"))
    assert f"MODELS_DIR={expected_models_dir}\n" in compose_text


def test_generated_media_credentials_are_valid_json_in_memory(tmp_path):
    server_dir = tmp_path / "server"
    server_dir.mkdir()
    model_source = tmp_path / "default.pt"
    model_source.write_bytes(b"model-content")
    result = initialize(
        InstallRequest(
            data_root=tmp_path / "data",
            server_dir=server_dir,
            admin_username="admin",
            admin_password="another-strong-password",
            model_path=model_source,
            cameras=[CameraBootstrap(camera_id="cam-001", name="One")],
        )
    )
    assert result.camera_credentials["cam-001"]["username"] == "cam-001"


def test_dotenv_quotes_dollar_values_without_compose_interpolation():
    assert _dotenv("$argon2id$v=19$hash") == "'$argon2id$v=19$hash'"
    assert _dotenv('{"cam-001":{"password":"secret"}}') == (
        '\'{"cam-001":{"password":"secret"}}\''
    )
    with pytest.raises(ValueError, match="single-line"):
        _dotenv("first\nsecond")


def test_public_base_url_accepts_only_an_https_origin():
    assert _validate_public_base_url("") == ""
    assert (
        _validate_public_base_url(" https://cctv.example.com:8443/ ")
        == "https://cctv.example.com:8443"
    )
    for invalid in (
        "http://cctv.example.com",
        "https://admin:secret@cctv.example.com",
        "https://cctv.example.com/api",
        "https://cctv.example.com?token=secret",
    ):
        with pytest.raises(ValueError, match="public base URL"):
            _validate_public_base_url(invalid)


def test_windows_private_file_acl_uses_account_and_well_known_sids(
    tmp_path, monkeypatch
):
    from configurator import private_files

    target = tmp_path / "secret.env"
    target.write_text("secret", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        if command == ["whoami"]:
            return subprocess.CompletedProcess(command, 0, stdout="DOMAIN\\operator\n")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(private_files.os, "name", "nt")
    monkeypatch.setattr(private_files.subprocess, "run", fake_run)

    private_files.restrict_private_file(target)

    assert calls[0] == ["whoami"]
    assert calls[1][:4] == [
        "icacls",
        str(target),
        "/inheritance:r",
        "/grant:r",
    ]
    assert "DOMAIN\\operator:(F)" in calls[1]
    assert "*S-1-5-18:(F)" in calls[1]
    assert "*S-1-5-32-544:(F)" in calls[1]


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
    inference = (output_dir / "inference.env").read_text(encoding="utf-8")
    media = (output_dir / "media.env").read_text(encoding="utf-8")
    scoped_tokens = {
        key: _env_value(data, key)
        for key in (
            "DATA_EXTERNAL_TOKEN",
            "DATA_INFERENCE_TOKEN",
            "DATA_MEDIA_TOKEN",
            "DATA_RECOVERY_TOKEN",
        )
    }
    assert len(set(scoped_tokens.values())) == 4
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
    assert len(restricted) == 4

    compose = Path("server/compose.yml").read_text(encoding="utf-8")
    assert "${DATA_SECRETS_FILE:-./secrets/data.env}" in compose
    assert "${EXTERNAL_SECRETS_FILE:-./secrets/external.env}" in compose
    assert "${INFERENCE_SECRETS_FILE:-./secrets/inference.env}" in compose
    assert "${MEDIA_SECRETS_FILE:-./secrets/media.env}" in compose
    assert "${SECRETS_FILE" not in compose
    assert "INTERNAL_CLIENT_SECRETS_FILE" not in compose


def _prepare_doctor_deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    server_dir = tmp_path / "server"
    for directory in (
        server_dir / "config",
        server_dir / "nginx",
        server_dir / "mediamtx",
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
        server_dir / "nginx" / "nginx.conf",
        server_dir / "mediamtx" / "mediamtx.yml",
        server_dir / "runtime" / "certificates" / "tls.crt",
        server_dir / "runtime" / "certificates" / "tls.key",
    ):
        path.write_text("test\n", encoding="utf-8")
    (server_dir / ".env").write_text(
        "DATA_SECRETS_FILE=./secrets/data.env\n"
        "EXTERNAL_SECRETS_FILE=./secrets/external.env\n"
        "INFERENCE_SECRETS_FILE=./secrets/inference.env\n"
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


def test_server_doctor_validates_scoped_tokens_and_secret_allowlists(
    tmp_path, monkeypatch, capsys
):
    server_dir = _prepare_doctor_deployment(tmp_path, monkeypatch)
    capsys.readouterr()
    monkeypatch.setattr(
        "sys.argv",
        ["doctor.py", "--server-dir", str(server_dir), "--skip-compose"],
    )
    assert server_doctor.main() == 0

    data_path = server_dir / "secrets" / "data.env"
    data_path.write_text(
        data_path.read_text(encoding="utf-8")
        + f'EDGE_AUTH_TOKENS_JSON=\'{{"edge-001":"{"e" * 40}"}}\'\n',
        encoding="utf-8",
    )
    assert server_doctor.main() == 0

    inference_path = server_dir / "secrets" / "inference.env"
    original = inference_path.read_text(encoding="utf-8")
    inference_path.write_text(
        original.replace(_env_value(original, "DATA_INFERENCE_TOKEN"), "m" * 40),
        encoding="utf-8",
    )
    assert server_doctor.main() == 1
    assert "matches between data.env and inference.env" in capsys.readouterr().out

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
        "MEDIA_READ_PASSWORD matches between external.env and inference.env"
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
        "MEDIA_READ_USERNAME matches between external.env and inference.env"
        in capsys.readouterr().out
    )

    inference_path.write_text(
        original.replace(_env_value(original, "MEDIA_READ_PASSWORD"), "too-short"),
        encoding="utf-8",
    )
    assert server_doctor.main() == 1
    assert (
        "inference.env contains a 32+ character MEDIA_READ_PASSWORD"
        in capsys.readouterr().out
    )


def test_configurator_rtsp_bind_defaults_to_loopback_and_requires_explicit_lan_ip():
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
    help_text = (
        build_parser().format_help()
        + build_parser()._subparsers._group_actions[0].choices["init"].format_help()
    )
    assert "trusted-LAN IP" in help_text
    gui_source = Path("configurator/gui.py").read_text(encoding="utf-8")
    assert 'self.rtsp_bind = QLineEdit("127.0.0.1")' in gui_source


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
        ["doctor.py", "--server-dir", str(server_dir), "--skip-compose"],
    )
    assert server_doctor.main() == 1
    output = capsys.readouterr().out
    assert "migrate SECRETS_FILE/INTERNAL_CLIENT_SECRETS_FILE" in output


def test_initialize_rejects_missing_or_unsupported_model(tmp_path):
    common = {
        "data_root": tmp_path / "data",
        "server_dir": tmp_path / "server",
        "admin_username": "admin",
        "admin_password": "another-strong-password",
        "cameras": [],
    }
    with pytest.raises(ValueError, match="does not exist"):
        initialize(InstallRequest(model_path=tmp_path / "missing.pt", **common))

    unsupported = tmp_path / "model.txt"
    unsupported.write_bytes(b"model-content")
    with pytest.raises(ValueError, match="supported model formats"):
        initialize(InstallRequest(model_path=unsupported, **common))


def test_local_model_install_is_atomic_bounded_and_manifest_free(
    tmp_path, monkeypatch
):
    source = tmp_path / "downloaded-model.onnx"
    source.write_bytes(b"locally-downloaded-model")
    installed = install_local_model(source, tmp_path / "persistent" / "models")

    assert installed.name == "downloaded-model.onnx"
    assert installed.read_bytes() == source.read_bytes()
    assert not list(installed.parent.glob(".*.tmp"))

    monkeypatch.setattr("configurator.model_manager.MAX_MODEL_BYTES", 4)
    with pytest.raises(ValueError, match="2 GiB size limit"):
        install_local_model(source, tmp_path / "other-models")


def test_initialize_copies_tls_and_compose_env_outside_server_package(
    tmp_path, monkeypatch
):
    server_dir = tmp_path / "read-only-server-package"
    server_dir.mkdir()
    model = tmp_path / "model.pt"
    model.write_bytes(b"model")
    certificate = tmp_path / "certificate.pem"
    certificate.write_text(
        "-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----\n",
        encoding="utf-8",
    )
    private_key = tmp_path / "private.key"
    private_key.write_text(
        "-----BEGIN PRIVATE KEY-----\ntest\n-----END PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    data_root = tmp_path / "program-data"
    compose_env = data_root / "config" / "compose.env"
    monkeypatch.setattr(
        "configurator.config_core._validate_certificate_key_match",
        lambda _certificate, _private_key: None,
    )

    result = initialize(
        InstallRequest(
            data_root=data_root,
            server_dir=server_dir,
            admin_username="admin",
            admin_password="a-strong-password",
            model_path=model,
            cameras=[],
            compose_env_path=compose_env,
            tls_certificate_path=certificate,
            tls_private_key_path=private_key,
        )
    )

    assert result.compose_env_path == compose_env.resolve()
    assert result.tls_certificate_path.read_bytes() == certificate.read_bytes()
    assert result.tls_private_key_path.read_bytes() == private_key.read_bytes()
    assert not (server_dir / ".env").exists()
    assert ComposeAdapter(server_dir, compose_env).env_file == compose_env.resolve()


def test_initialize_rejects_incomplete_or_encrypted_tls_pair(tmp_path):
    model = tmp_path / "model.pt"
    model.write_bytes(b"model")
    certificate = tmp_path / "certificate.pem"
    certificate.write_text(
        "-----BEGIN CERTIFICATE-----\ntest\n-----END CERTIFICATE-----\n",
        encoding="utf-8",
    )
    encrypted_key = tmp_path / "private.key"
    encrypted_key.write_text(
        "-----BEGIN ENCRYPTED PRIVATE KEY-----\ntest\n"
        "-----END ENCRYPTED PRIVATE KEY-----\n",
        encoding="utf-8",
    )
    common = {
        "data_root": tmp_path / "data",
        "server_dir": tmp_path / "server",
        "admin_username": "admin",
        "admin_password": "a-strong-password",
        "model_path": model,
        "cameras": [],
        "tls_certificate_path": certificate,
    }

    with pytest.raises(ValueError, match="must be provided together"):
        initialize(InstallRequest(**common))
    with pytest.raises(ValueError, match="unencrypted PEM"):
        initialize(InstallRequest(tls_private_key_path=encrypted_key, **common))


def test_frozen_configurator_discovers_programdata_compose_env(
    tmp_path, monkeypatch
):
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


def test_cli_init_reports_input_error_without_traceback(tmp_path, capsys):
    password = tmp_path / "admin-password.txt"
    password.write_text("a-strong-password", encoding="utf-8")
    result = configurator_cli.main(
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


def test_gui_and_frozen_cli_expose_consumer_local_model_flow():
    gui_source = Path("configurator/gui.py").read_text(encoding="utf-8")
    assert "Choose model manifest" not in gui_source
    assert "self.cameras = QLineEdit()" in gui_source
    assert "tls_certificate_path" in gui_source
    init_help = build_parser()._subparsers._group_actions[0].choices[
        "init"
    ].format_help()
    assert "--model MODEL" in init_help
    assert "--model-manifest" not in init_help
    assert build_parser().parse_args(
        ["--env-file", "before.env", "stop"]
    ).env_file == Path("before.env")
    assert build_parser().parse_args(
        ["stop", "--env-file", "after.env"]
    ).env_file == Path("after.env")
    cli_spec = Path("configurator/packaging/ai_cctv_cli.spec").read_text(
        encoding="utf-8"
    )
    assert 'name="AI_CCTV_CLI"' in cli_spec
    assert "console=True" in cli_spec
    gui_spec = Path("configurator/packaging/ai_cctv_configurator.spec").read_text(
        encoding="utf-8"
    )
    assert "uac_admin=True" in gui_spec


class _JsonResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


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


def test_server_api_registers_edge_and_manages_profile_without_query_tokens():
    opener = _QueuedOpener(
        {
            "access_token": "access-secret",
            "refresh_token": "refresh-secret",
            "token_type": "bearer",
        },
        {"camera_id": "cam-001", "edge_device_id": "edge-001"},
        {
            "camera_id": "cam-001",
            "current_profile": "hd",
            "desired_profile": "hd",
            "supported_profiles": ["hd", "fhd"],
            "edge_online": True,
        },
        {
            "camera_id": "cam-001",
            "current_profile": "fhd",
            "desired_profile": "fhd",
            "supported_profiles": ["hd", "fhd"],
            "edge_online": True,
        },
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
    client.video_profile("cam-001")
    client.set_video_profile("cam-001", "fhd")

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
    assert "?" not in opener.requests[2][0].full_url
    profile_request = opener.requests[3][0]
    assert profile_request.method == "PATCH"
    assert json.loads(profile_request.data) == {"profile": "fhd"}
    assert opener.requests[3][1] == 90.0


def test_server_api_status_and_safe_profile_error_are_operator_facing():
    failure_body = json.dumps(
        {
            "error": {
                "code": "UNSUPPORTED_VIDEO_PROFILE",
                "message": "This Edge does not support FHD at 30fps.",
                "details": {
                    "requested_profile": "fhd",
                    "supported_profiles": ["hd"],
                },
            }
        }
    ).encode("utf-8")
    failure = HTTPError(
        "https://cctv.example.com/api/v1/cameras/cam-001/video-profile",
        409,
        "Conflict",
        {},
        io.BytesIO(failure_body),
    )
    opener = _QueuedOpener(
        {"access_token": "access-secret"},
        {
            "camera_id": "cam-001",
            "online": True,
            "current_video_profile": "hd",
            "last_seen_at": "2026-08-23T07:20:00Z",
        },
        failure,
    )
    client = ServerApiClient("https://cctv.example.com", opener=opener)
    client.login("admin", "administrator-password")
    status = client.camera_status("cam-001")
    assert status["last_seen_at"].endswith("Z")

    with pytest.raises(ServerApiError) as captured:
        client.set_video_profile("cam-001", "fhd")
    assert captured.value.status_code == 409
    assert captured.value.code == "UNSUPPORTED_VIDEO_PROFILE"
    assert "FHD" in captured.value.message


def test_configurator_redacts_nested_credentials_and_rejects_unsafe_server_url():
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
    with pytest.raises(ValueError, match="video profile"):
        ServerApiClient(
            "https://cctv.example.com", opener=_QueuedOpener()
        ).set_video_profile("cam-001", "4k")
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

    monkeypatch.setattr("configurator.server_api.build_opener", fake_build_opener)
    ServerApiClient("https://cctv.example.com")

    proxy_handlers = [item for item in captured if isinstance(item, ProxyHandler)]
    assert len(proxy_handlers) == 1
    assert proxy_handlers[0].proxies == {}


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

    monkeypatch.setattr(configurator_cli, "ServerApiClient", FakeClient)
    monkeypatch.setattr(
        "configurator.server_api.restrict_private_file",
        lambda path: protected_paths.append(path),
    )
    result = configurator_cli.main(
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


def test_edge_credential_rotation_cli_writes_only_private_handoff(
    tmp_path, monkeypatch, capsys
):
    password_file = tmp_path / "admin-password.txt"
    password_file.write_text("administrator-password", encoding="utf-8")
    handoff_file = tmp_path / "cam-001-rotated.json"
    calls = []

    class FakeClient:
        def __init__(self, base_url):
            calls.append(("base", base_url))

        def login(self, username, password):
            calls.append(("login", username, password))
            return {"access_token": "[redacted]"}

        def rotate_publish_credentials(self, camera_id):
            calls.append(("rotate", camera_id))
            return {
                "camera_id": camera_id,
                "publish_credentials": {
                    "username": camera_id,
                    "password": "new-publish-secret",
                },
            }

    monkeypatch.setattr(configurator_cli, "ServerApiClient", FakeClient)
    monkeypatch.setattr(
        "configurator.server_api.restrict_private_file", lambda _path: None
    )

    result = configurator_cli.main(
        [
            "edge-rotate-credentials",
            "cam-001",
            "--server-url",
            "https://cctv.example.com",
            "--password-file",
            str(password_file),
            "--publish-credentials-output",
            str(handoff_file),
        ]
    )

    assert result == 0
    assert ("rotate", "cam-001") in calls
    assert json.loads(handoff_file.read_text(encoding="utf-8"))["password"] == (
        "new-publish-secret"
    )
    assert "new-publish-secret" not in capsys.readouterr().out


def test_gui_credential_rotation_requires_safe_handoff_and_redacts_secret(
    tmp_path, monkeypatch, capsys
):
    handoff_file = tmp_path / "gui" / "cam-001-rotated.json"
    calls = []

    class FakeClient:
        def rotate_publish_credentials(self, camera_id):
            calls.append(("rotate", camera_id, handoff_file.parent.exists()))
            return {
                "camera_id": camera_id,
                "publish_credentials": {
                    "username": camera_id,
                    "password": "gui-new-publish-secret",
                },
            }

    monkeypatch.setattr(
        "configurator.server_api.restrict_private_file", lambda _path: None
    )

    with pytest.raises(ValueError, match="camera ID is required"):
        rotate_publish_credentials_to_file(FakeClient(), "  ", handoff_file)
    with pytest.raises(ValueError, match="output path is required"):
        rotate_publish_credentials_to_file(FakeClient(), "cam-001", "  ")
    assert calls == []

    result = rotate_publish_credentials_to_file(FakeClient(), " cam-001 ", handoff_file)

    assert calls == [("rotate", "cam-001", True)]
    assert json.loads(handoff_file.read_text(encoding="utf-8")) == {
        "camera_id": "cam-001",
        "username": "cam-001",
        "password": "gui-new-publish-secret",
    }
    assert result["handoff_file"] == str(handoff_file.resolve())
    displayed = json.dumps(redact_for_display(result), ensure_ascii=False)
    assert "gui-new-publish-secret" not in displayed
    assert "gui-new-publish-secret" not in capsys.readouterr().out

    gui_source = Path("configurator/gui.py").read_text(encoding="utf-8")
    assert (
        "rotate_credentials.clicked.connect(self.rotate_publish_credentials)"
        in gui_source
    )
