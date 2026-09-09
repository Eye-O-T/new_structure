# 공통 초기화의 설정 생성·모델 복사·TLS 입력·서비스별 비밀값 분리 계약을 검증한다.
import hashlib
import json
import os
import stat
import subprocess

import pytest

from ai_cctv_core.config import CameraBootstrap, load_config
from server.setup.config_core import (
    InstallRequest,
    _dotenv,
    _validate_public_base_url,
    initialize,
)
from server.setup.model_manager import install_local_model


def _env_value(text: str, key: str) -> str:
    return next(
        line.partition("=")[2].strip("'")
        for line in text.splitlines()
        if line.startswith(f"{key}=")
    )


# 설정·모델·서비스별 인증 파일을 실제로 생성해 권한 분리와 평문 관리자 암호 미저장을 확인한다.
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
            recording_segment_seconds=45,
            retention_days=14,
            storage_warning_free_percent=20,
            inference_device="cpu",
        )
    )
    config = load_config(result.config_path)
    assert config.cameras[0].stream_path == "cam-001"
    assert config.inference.model_path == "/models/selected-model.pt"
    assert config.inference.device == "cpu"
    assert config.recording.segment_seconds == 45
    assert config.recording.retention_days == 14
    assert config.recording.warning_free_percent == 20
    assert (tmp_path / "data" / "models" / "selected-model.pt").read_bytes() == (
        b"model-content"
    )

    data_secrets = result.secrets_path.read_text(encoding="utf-8")
    external_secrets = result.external_secrets_path.read_text(encoding="utf-8")
    preprocessing_secrets = result.preprocessing_secrets_path.read_text(
        encoding="utf-8"
    )
    analysis_secrets = result.analysis_secrets_path.read_text(encoding="utf-8")
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
    assert "DATA_INFERENCE_TOKEN=" in preprocessing_secrets
    assert media_secrets.startswith("DATA_MEDIA_TOKEN=")
    assert "JWT_SECRET=" not in preprocessing_secrets
    assert "JWT_SECRET=" not in media_secrets
    assert "MEDIA_READ_USERNAME=" not in media_secrets
    assert "MEDIA_READ_PASSWORD=" not in media_secrets
    assert _env_value(external_secrets, "MEDIA_READ_USERNAME") == _env_value(
        preprocessing_secrets, "MEDIA_READ_USERNAME"
    )
    assert _env_value(external_secrets, "MEDIA_READ_PASSWORD") == _env_value(
        preprocessing_secrets, "MEDIA_READ_PASSWORD"
    )
    assert len(_env_value(preprocessing_secrets, "MEDIA_READ_PASSWORD")) >= 32
    scoped_tokens = {
        key: _env_value(data_secrets, key)
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
        _env_value(preprocessing_secrets, "DATA_IDENTITY_TOKEN")
        == scoped_tokens["DATA_IDENTITY_TOKEN"]
    )
    assert (
        _env_value(analysis_secrets, "DATA_ANALYSIS_TOKEN")
        == scoped_tokens["DATA_ANALYSIS_TOKEN"]
    )
    assert (
        _env_value(external_secrets, "DATA_EXTERNAL_TOKEN")
        == scoped_tokens["DATA_EXTERNAL_TOKEN"]
    )
    assert (
        _env_value(preprocessing_secrets, "DATA_INFERENCE_TOKEN")
        == scoped_tokens["DATA_INFERENCE_TOKEN"]
    )
    assert (
        _env_value(media_secrets, "DATA_MEDIA_TOKEN")
        == scoped_tokens["DATA_MEDIA_TOKEN"]
    )
    assert "INTERNAL_SERVICE_TOKEN=" not in data_secrets
    # Windows ACL은 POSIX 권한 비트로 확인할 수 없으므로 해당 검사는 POSIX에서만 수행한다.
    if os.name != "nt":
        assert stat.S_IMODE(result.secrets_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(result.external_secrets_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(result.preprocessing_secrets_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(result.analysis_secrets_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(result.media_secrets_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(result.camera_credentials_path.stat().st_mode) == 0o600
    assert result.camera_credentials_path.read_text(encoding="utf-8").startswith("{")
    assert (
        result.camera_credentials["cam-001"]["password"] not in config.model_dump_json()
    )

    compose_text = result.compose_env_path.read_text(encoding="utf-8")
    assert "AI_CCTV_VERSION=0.3.0\n" in compose_text
    assert "RECORDING_SEGMENT_SECONDS=45\n" in compose_text
    assert "MODEL_FILE=selected-model.pt\n" in compose_text
    assert "RTSP_BIND_ADDRESS=127.0.0.1\n" in compose_text
    assert "PUBLIC_BASE_URL=https://cctv.example.com\n" in compose_text
    assert "DATA_SECRETS_FILE=" in compose_text
    assert "EXTERNAL_SECRETS_FILE=" in compose_text
    assert "PREPROCESSING_SECRETS_FILE=" in compose_text
    assert "ANALYSIS_SECRETS_FILE=" in compose_text
    assert "\nINFERENCE_SECRETS_FILE=" not in compose_text
    assert "\nIDENTITY_SECRETS_FILE=" not in compose_text
    assert "MEDIA_SECRETS_FILE=" in compose_text
    assert "\nSECRETS_FILE=" not in compose_text
    expected_models_dir = _dotenv(str(tmp_path / "data" / "models"))
    assert f"MODELS_DIR={expected_models_dir}\n" in compose_text
    manifest = json.loads(result.release_manifest_path.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == 1
    assert manifest["application_version"] == "0.3.0"
    assert manifest["images"]["mediamtx"] == "ai-cctv-mediamtx:1.9.0"
    assert manifest["images"]["preprocessing"] == "ai-cctv-preprocessing:0.3.0"
    assert manifest["images"]["analysis"] == "ai-cctv-analysis:0.3.0"
    assert "inference" not in manifest["images"]
    assert manifest["model"] == {
        "filename": "selected-model.pt",
        "sha256": hashlib.sha256(b"model-content").hexdigest(),
    }


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


# Argon2 해시의 달러 기호가 Compose 환경 변수 치환으로 손상되지 않는 인용 규칙을 고정한다.
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


# Windows 명령만 대체해 설치 계정과 언어에 독립적인 관리자·SYSTEM SID로 ACL을 구성하는지 확인한다.
def test_windows_private_file_acl_uses_account_and_well_known_sids(
    tmp_path, monkeypatch
):
    from server.setup import private_files

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


# 유효하지 않은 런타임 설정은 데이터 폴더를 만들기 전에 거부되어야 한다.
@pytest.mark.parametrize(
    ("field", "value", "message"),
    (
        ("recording_segment_seconds", 9, "segment seconds"),
        ("recording_segment_seconds", 301, "segment seconds"),
        ("retention_days", 0, "retention days"),
        ("storage_warning_free_percent", 0, "warning free percent"),
        ("inference_device", "gpu-zero", "inference device"),
        ("public_http_port", 0, "ports must be"),
        ("public_https_port", 8554, "ports must be distinct"),
    ),
)
def test_initialize_rejects_invalid_runtime_settings_before_writing(
    tmp_path, field, value, message
):
    model = tmp_path / "model.pt"
    model.write_bytes(b"model")
    values = {
        "data_root": tmp_path / "data",
        "server_dir": tmp_path / "server",
        "admin_username": "admin",
        "admin_password": "another-strong-password",
        "model_path": model,
        "cameras": [],
        field: value,
    }

    with pytest.raises(ValueError, match=message):
        initialize(InstallRequest(**values))

    assert not (tmp_path / "data").exists()


# 모델 복사 내용·임시 파일 정리·크기 제한을 검증하며 실제 추론 엔진은 실행하지 않는다.
def test_local_model_install_is_atomic_bounded_and_manifest_free(tmp_path, monkeypatch):
    source = tmp_path / "downloaded-model.onnx"
    source.write_bytes(b"locally-downloaded-model")
    installed = install_local_model(source, tmp_path / "persistent" / "models")

    assert installed.name == "downloaded-model.onnx"
    assert installed.read_bytes() == source.read_bytes()
    assert not list(installed.parent.glob(".*.tmp"))

    monkeypatch.setattr("server.setup.model_manager.MAX_MODEL_BYTES", 4)
    with pytest.raises(ValueError, match="2 GiB size limit"):
        install_local_model(source, tmp_path / "other-models")


# TLS 짝 검사는 대체하고 인증서·env가 프로그램 폴더 밖의 영구 저장소에 배치되는지 확인한다.
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
        "server.setup.config_core._validate_certificate_key_match",
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


# 인증서만 지정하거나 암호화된 개인키를 지정하면 초기화를 거부해야 한다.
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
