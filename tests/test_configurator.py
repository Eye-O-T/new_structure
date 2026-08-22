import hashlib
import io
import json
import stat

import pytest

from ai_cctv_core.config import CameraBootstrap, load_config
from configurator.config_core import InstallRequest, _dotenv, initialize
from configurator.model_manager import install_from_manifest


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
        )
    )
    config = load_config(result.config_path)
    assert config.cameras[0].stream_path == "cam-001"
    assert config.inference.model_path == "/models/selected-model.pt"
    assert (tmp_path / "data" / "models" / "selected-model.pt").read_bytes() == (
        b"model-content"
    )

    secrets_text = result.secrets_path.read_text(encoding="utf-8")
    assert "a-strong-password" not in secrets_text
    assert "INITIAL_ADMIN_PASSWORD_HASH='$argon2" in secrets_text
    assert "JWT_SECRET=" in secrets_text
    assert stat.S_IMODE(result.secrets_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(result.camera_credentials_path.stat().st_mode) == 0o600
    assert result.camera_credentials_path.read_text(encoding="utf-8").startswith("{")
    assert (
        result.camera_credentials["cam-001"]["password"] not in config.model_dump_json()
    )

    compose_text = result.compose_env_path.read_text(encoding="utf-8")
    assert "MODEL_FILE=selected-model.pt\n" in compose_text
    assert "RTSP_BIND_ADDRESS=0.0.0.0\n" in compose_text
    assert f"MODELS_DIR={tmp_path / 'data' / 'models'}\n" in compose_text


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


def test_manifest_model_install_verifies_https_hash_and_path(tmp_path, monkeypatch):
    content = b"verified-model"

    class Response(io.BytesIO):
        headers = {"Content-Length": str(len(content))}

        def geturl(self):
            return "https://models.example/default.pt"

        def __enter__(self):
            return self

        def __exit__(self, *_):
            self.close()

    monkeypatch.setattr(
        "configurator.model_manager.urlopen",
        lambda *_args, **_kwargs: Response(content),
    )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "name": "default.pt",
                "version": "1.0.0",
                "url": "https://models.example/default.pt",
                "sha256": hashlib.sha256(content).hexdigest(),
                "license": "approved-test-license",
            }
        ),
        encoding="utf-8",
    )
    installed = install_from_manifest(manifest, tmp_path / "models")
    assert installed.read_bytes() == content

    unsafe = json.loads(manifest.read_text(encoding="utf-8"))
    unsafe["name"] = "../escape.pt"
    manifest.write_text(json.dumps(unsafe), encoding="utf-8")
    with pytest.raises(ValueError, match="name"):
        install_from_manifest(manifest, tmp_path / "models")
