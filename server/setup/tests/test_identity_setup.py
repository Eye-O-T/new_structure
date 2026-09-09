"""OSNet 설치 파일 연결과 기존 사용자 설정 보존을 실제 파일로 검증한다."""

import hashlib
import json
from dataclasses import replace

import pytest

from server.setup.config_core import InstallRequest, initialize
from server.setup.model_manager import (
    GENERIC_IDENTITY_PLUGIN,
    IDENTITY_MODEL_CONTAINER_PATH,
    IDENTITY_MODEL_NAME,
    IDENTITY_PLUGIN,
    deployed_identity_model,
    resolve_identity_model,
)
from server.setup.install_helper.cli import build_parser
from server.setup.install_helper import compose_adapter
from server.setup import model_manager
from server.setup.install_helper import doctor
from server.setup.tools.enable_object_processing import enable
from server.setup.validation import read_deployment_env


@pytest.fixture
def install_request(tmp_path):
    detector = tmp_path / "detector.pt"
    detector.write_bytes(b"detector-presence-only")
    return InstallRequest(
        data_root=tmp_path / "persistent",
        server_dir=tmp_path / "server",
        admin_username="operator",
        admin_password="test-password-123",
        model_path=detector,
        cameras=[],
    )


def test_missing_osnet_stops_initialization_before_writing_deployment(install_request):
    with pytest.raises(ValueError, match="prepare_osnet.py") as failure:
        initialize(install_request)
    assert "--output" in str(failure.value)
    assert not install_request.data_root.exists()
    assert not install_request.server_dir.exists()


@pytest.mark.parametrize("detector_name", ["shared.onnx", "SHARED.ONNX", "Shared.Onnx"])
def test_model_filename_collision_is_rejected_before_any_deployment_write(
    install_request, tmp_path, detector_name
):
    detector = tmp_path / "detection" / detector_name
    identity = tmp_path / "identity" / "shared.onnx"
    detector.parent.mkdir()
    identity.parent.mkdir()
    detector.write_bytes(b"detection-model")
    identity.write_bytes(b"identity-model")
    with pytest.raises(ValueError, match="different filenames"):
        initialize(
            replace(install_request, model_path=detector, identity_model_path=identity)
        )
    assert not install_request.data_root.exists()
    assert not install_request.server_dir.exists()
    assert detector.read_bytes() == b"detection-model"
    assert identity.read_bytes() == b"identity-model"


def test_identity_selection_preserves_filename_and_copies_license_and_provenance(
    install_request, tmp_path
):
    identity = tmp_path / "site_osnet.onnx"
    identity.write_bytes(b"identity-presence-only")
    identity.with_suffix(".onnx.json").write_text(
        '{"checkpoint":"test"}', encoding="utf-8"
    )
    identity.with_suffix(".LICENSE.txt").write_text("test license", encoding="utf-8")
    result = initialize(replace(install_request, identity_model_path=identity))
    environment = read_deployment_env(result.compose_env_path)
    installed = install_request.data_root / "models" / identity.name
    assert installed.read_bytes() == identity.read_bytes()
    assert environment["MODEL_FILE"] == "detector.pt"
    assert environment["IDENTITY_PLUGIN"] == IDENTITY_PLUGIN
    assert environment["IDENTITY_MODEL_PATH"] == "/models/site_osnet.onnx"
    assert environment["IDENTITY_MATCH_THRESHOLD"] == "0.97"
    assert environment["IDENTITY_MATCH_MARGIN"] == "0.05"
    for suffix in (".onnx.json", ".LICENSE.txt"):
        assert (
            installed.with_suffix(suffix).read_bytes()
            == identity.with_suffix(suffix).read_bytes()
        )
    manifest = json.loads(result.release_manifest_path.read_text(encoding="utf-8"))
    assert manifest["identity_model"] == {
        "plugin": IDENTITY_PLUGIN,
        "filename": identity.name,
        "sha256": hashlib.sha256(identity.read_bytes()).hexdigest(),
    }
    assert "IDENTITY_PLUGIN" not in read_deployment_env(
        result.preprocessing_secrets_path
    )


def test_new_initialization_keeps_explicit_matching_calibration(
    install_request, tmp_path
):
    identity = tmp_path / "site_osnet.onnx"
    identity.write_bytes(b"identity-presence-only")
    install_request.server_dir.mkdir()
    (install_request.server_dir / ".env").write_text(
        "IDENTITY_MATCH_THRESHOLD=0.88\nIDENTITY_MATCH_MARGIN=0.12\n", encoding="utf-8"
    )
    result = initialize(replace(install_request, identity_model_path=identity))
    values = read_deployment_env(result.compose_env_path)
    assert values["IDENTITY_MATCH_THRESHOLD"] == "0.88"
    assert values["IDENTITY_MATCH_MARGIN"] == "0.12"


def test_default_model_lookup_prioritizes_persistent_then_runtime_then_package(
    install_request,
):
    candidates = [
        install_request.data_root / "models" / IDENTITY_MODEL_NAME,
        install_request.server_dir / "runtime" / "models" / IDENTITY_MODEL_NAME,
        install_request.server_dir / "models" / IDENTITY_MODEL_NAME,
    ]
    for candidate in reversed(candidates):
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate.write_bytes(b"identity-presence-only")
        assert (
            resolve_identity_model(
                None, install_request.data_root, install_request.server_dir
            )
            == candidate.resolve()
        )


def test_explicit_bad_identity_is_not_replaced_with_a_different_model(
    install_request, tmp_path
):
    fallback = install_request.server_dir / "runtime" / "models" / IDENTITY_MODEL_NAME
    fallback.parent.mkdir(parents=True)
    fallback.write_bytes(b"fallback")
    for selected in [tmp_path / "missing.onnx", install_request.model_path]:
        with pytest.raises(ValueError):
            resolve_identity_model(
                selected, install_request.data_root, install_request.server_dir
            )


def test_identity_size_limit_matches_selection_start_checks_and_doctor(
    install_request, tmp_path, monkeypatch
):
    identity = tmp_path / "site_osnet.onnx"
    identity.write_bytes(b"model")
    result = initialize(replace(install_request, identity_model_path=identity))
    adapter = compose_adapter.ComposeAdapter(
        install_request.server_dir, result.compose_env_path
    )
    monkeypatch.setattr(compose_adapter, "installation_prerequisites", lambda _: [])
    monkeypatch.setattr(model_manager, "MAX_IDENTITY_MODEL_BYTES", 4)
    with pytest.raises(ValueError, match="256 MiB"):
        resolve_identity_model(
            identity, install_request.data_root, install_request.server_dir
        )
    assert not next(
        item
        for item in adapter.deployment_prerequisites()
        if item.name == "OSNet identity model"
    ).ok
    assert (
        next(
            item
            for item in doctor._configuration_checks(adapter, result.config_path)
            if item.name == "OSNet identity model"
        ).status
        == "ERROR"
    )


@pytest.mark.parametrize(
    "raw", ["/other/model.onnx", "/models/../outside.onnx", "relative.onnx", "/models"]
)
def test_deployment_identity_path_cannot_escape_models_mount(tmp_path, raw):
    assert deployed_identity_model({"IDENTITY_MODEL_PATH": raw}, tmp_path) is None


def test_osnet_missing_or_empty_file_blocks_start_but_custom_plugin_is_preserved(
    tmp_path, monkeypatch
):
    server = tmp_path / "server"
    server.mkdir()
    models = server / "models"
    models.mkdir()
    env = server / ".env"
    env.write_text(f"MODELS_DIR={models.as_posix()}\n", encoding="utf-8")
    monkeypatch.setattr(compose_adapter, "installation_prerequisites", lambda _: [])
    adapter = compose_adapter.ComposeAdapter(server, env)

    def identity_check():
        return next(
            item
            for item in adapter.deployment_prerequisites()
            if item.name == "OSNet identity model"
        )

    assert not identity_check().ok
    path = models / IDENTITY_MODEL_NAME
    path.touch()
    assert not identity_check().ok
    path.write_bytes(b"identity-presence-only")
    assert identity_check().ok
    env.write_text("IDENTITY_PLUGIN=site.processor:Factory\n", encoding="utf-8")
    assert not any(
        item.name == "OSNet identity model"
        for item in adapter.deployment_prerequisites()
    )


@pytest.mark.parametrize(
    "plugin,model,expected_plugin,expected_model",
    [
        (None, None, IDENTITY_PLUGIN, IDENTITY_MODEL_CONTAINER_PATH),
        (GENERIC_IDENTITY_PLUGIN, "", IDENTITY_PLUGIN, IDENTITY_MODEL_CONTAINER_PATH),
        (
            "app.plugins:IdentityBlackBox",
            None,
            IDENTITY_PLUGIN,
            IDENTITY_MODEL_CONTAINER_PATH,
        ),
        (
            GENERIC_IDENTITY_PLUGIN,
            "/models/custom.onnx",
            GENERIC_IDENTITY_PLUGIN,
            "/models/custom.onnx",
        ),
        (None, "/models/custom.onnx", GENERIC_IDENTITY_PLUGIN, "/models/custom.onnx"),
        ("site.processor:Factory", None, "site.processor:Factory", ""),
        (
            "site.processor:Factory",
            "/custom.onnx",
            "site.processor:Factory",
            "/custom.onnx",
        ),
        (
            IDENTITY_PLUGIN,
            "/models/site_osnet.onnx",
            IDENTITY_PLUGIN,
            "/models/site_osnet.onnx",
        ),
    ],
)
def test_migration_updates_only_known_defaults_and_preserves_custom_settings(
    tmp_path, plugin, model, expected_plugin, expected_model
):
    server = tmp_path / "server"
    secrets = server / "secrets"
    secrets.mkdir(parents=True)
    tokens = {
        f"DATA_{scope}_TOKEN": code * 40
        for scope, code in {
            "EXTERNAL": "e",
            "INFERENCE": "i",
            "MEDIA": "m",
            "RECOVERY": "r",
            "IDENTITY": "d",
            "ANALYSIS": "a",
        }.items()
    }
    (secrets / "data.env").write_text(
        "".join(f"{key}={value}\n" for key, value in tokens.items()), encoding="utf-8"
    )
    (secrets / "preprocessing.env").write_text(
        f"DATA_INFERENCE_TOKEN={tokens['DATA_INFERENCE_TOKEN']}\n"
        f"DATA_IDENTITY_TOKEN={tokens['DATA_IDENTITY_TOKEN']}\n"
        "MEDIA_READ_USERNAME=reader\nMEDIA_READ_PASSWORD=" + "x" * 40 + "\n",
        encoding="utf-8",
    )
    env = server / ".env"
    content = "# keep my deployment\nDATA_SECRETS_FILE=./secrets/data.env\nIDENTITY_MATCH_THRESHOLD=0.83\nIDENTITY_MATCH_MARGIN=0.09\n"
    if plugin is not None:
        content += f"IDENTITY_PLUGIN={plugin}\n"
    if model is not None:
        content += f"IDENTITY_MODEL_PATH={model}\n"
    env.write_text(content, encoding="utf-8")
    enable(server, env)
    values = read_deployment_env(env)
    assert values["IDENTITY_PLUGIN"] == expected_plugin
    assert values["IDENTITY_MODEL_PATH"] == expected_model
    assert values["IDENTITY_MATCH_THRESHOLD"] == "0.83"
    assert values["IDENTITY_MATCH_MARGIN"] == "0.09"
    assert "# keep my deployment" in env.read_text(encoding="utf-8")
    assert read_deployment_env(secrets / "data.env") == tokens
    first = env.read_bytes()
    enable(server, env)
    assert env.read_bytes() == first


def test_cli_accepts_separate_detection_and_identity_models(tmp_path):
    parsed = build_parser().parse_args(
        [
            "init",
            "--model",
            str(tmp_path / "detector.pt"),
            "--identity-model",
            str(tmp_path / "site_osnet.onnx"),
        ]
    )
    assert parsed.identity_model.name == "site_osnet.onnx"
    assert parsed.model.name == "detector.pt"
