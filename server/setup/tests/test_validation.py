"""배포 파일 검사는 GUI·실행 서비스 없이 인증 계약을 확인한다."""

import json
from pathlib import Path

import pytest

from server.setup.validation import (
    deployment_path,
    read_deployment_env,
    validate_deployment,
)


# Windows 역슬래시와 공백을 보존하는 JSON 인용 규칙으로 테스트 env를 작성한다.
def write_env(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(f"{key}={json.dumps(value)}\n" for key, value in values.items()),
        encoding="utf-8",
    )


# 외부 서비스 없이 검사할 수 있는 파일 구조와 서로 일치하는 역할별 토큰의 기준 배포를 만든다.
def make_deployment(tmp_path: Path) -> tuple[Path, Path, Path, dict[str, Path]]:
    server = tmp_path / "server"
    env = tmp_path / "deployment" / "compose.env"
    config = server / "config" / "config.yaml"
    for relative in (
        "compose.yml",
        "config/config.yaml",
        "services/nginx/nginx.conf",
        "services/mediamtx/mediamtx.yml",
        "runtime/certificates/tls.crt",
        "runtime/certificates/tls.key",
    ):
        target = server / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("file-existence-only\n", encoding="utf-8")
    for directory in (
        "database",
        "recordings",
        "recovered",
        "snapshots",
        "models",
        "logs",
    ):
        (server / "runtime" / directory).mkdir(parents=True, exist_ok=True)
    tokens = {
        f"DATA_{role.upper()}_TOKEN": role + "-token-" + "x" * 40
        for role in (
            "external",
            "inference",
            "identity",
            "analysis",
            "media",
            "recovery",
        )
    }
    media = {"MEDIA_READ_USERNAME": "reader", "MEDIA_READ_PASSWORD": "p" * 40}
    values = {
        "data": tokens,
        "external": {
            "DATA_EXTERNAL_TOKEN": tokens["DATA_EXTERNAL_TOKEN"],
            "JWT_SECRET": "j" * 40,
            **media,
        },
        "preprocessing": {
            "DATA_INFERENCE_TOKEN": tokens["DATA_INFERENCE_TOKEN"],
            "DATA_IDENTITY_TOKEN": tokens["DATA_IDENTITY_TOKEN"],
            **media,
        },
        "analysis": {"DATA_ANALYSIS_TOKEN": tokens["DATA_ANALYSIS_TOKEN"]},
        "media": {"DATA_MEDIA_TOKEN": tokens["DATA_MEDIA_TOKEN"]},
    }
    secret_paths = {}
    environment = {"CONFIG_FILE": "./config/config.yaml"}
    for role, secrets in values.items():
        target = tmp_path / "deployment" / "secrets" / f"{role}.env"
        write_env(target, secrets)
        secret_paths[role] = target
        environment[f"{role.upper()}_SECRETS_FILE"] = str(target)
    write_env(env, environment)
    return server, env, config, secret_paths


# 별도 env의 경로를 읽되 비밀값은 진단 결과와 콘솔 모두에 노출하지 않아야 한다.
def test_custom_environment_paths_and_tokens_are_checked_without_output(
    tmp_path, capsys
):
    server, env, config, secrets = make_deployment(tmp_path)
    results = validate_deployment(server, env_file=env)
    assert results and all(result.status == "OK" for result in results)
    assert capsys.readouterr().out == ""
    assert not (server / ".env").exists()
    assert any(str(config) in result.message for result in results)
    assert all(
        token not in repr(results)
        for token in read_deployment_env(secrets["data"]).values()
    )


# 역할·길이·소비자 간 일치 조건을 하나씩 깨뜨려 원인만 보고하고 해당 값은 숨기는지 확인한다.
@pytest.mark.parametrize(
    ("role", "key", "value", "error"),
    [
        (
            "analysis",
            "DATA_ANALYSIS_TOKEN",
            "m" * 40,
            "matches between data.env and analysis.env",
        ),
        (
            "preprocessing",
            "DATA_INFERENCE_TOKEN",
            "short",
            "32+ character DATA_INFERENCE_TOKEN",
        ),
        (
            "preprocessing",
            "DATA_EXTERNAL_TOKEN",
            "x" * 40,
            "forbidden: DATA_EXTERNAL_TOKEN",
        ),
        ("external", "JWT_SECRET", "short", "32+ byte JWT_SECRET"),
        (
            "preprocessing",
            "MEDIA_READ_USERNAME",
            "other-reader",
            "MEDIA_READ_USERNAME matches",
        ),
        (
            "preprocessing",
            "MEDIA_READ_PASSWORD",
            "q" * 40,
            "MEDIA_READ_PASSWORD matches",
        ),
        (
            "external",
            "MEDIA_READ_PASSWORD",
            "short",
            "32+ character MEDIA_READ_PASSWORD",
        ),
        (
            "data",
            "DATA_IDENTITY_TOKEN",
            "replace-with" + "x" * 40,
            "does not contain placeholders",
        ),
    ],
)
def test_secret_contract_failures_are_reported(tmp_path, role, key, value, error):
    server, env, _, secrets = make_deployment(tmp_path)
    values = read_deployment_env(secrets[role])
    values[key] = value
    write_env(secrets[role], values)
    results = validate_deployment(server, env_file=env)
    assert any(
        result.status == "ERROR" and error in result.message for result in results
    )
    assert value not in repr(results)


# 토큰 재사용과 여러 파일 누락이 동시에 있어도 첫 오류에서 진단이 끝나지 않아야 한다.
def test_duplicate_tokens_and_missing_paths_are_reported_together(tmp_path):
    server, env, _, secrets = make_deployment(tmp_path)
    values = read_deployment_env(secrets["data"])
    values["DATA_IDENTITY_TOKEN"] = values["DATA_INFERENCE_TOKEN"]
    write_env(secrets["data"], values)
    (server / "runtime/certificates/tls.key").unlink()
    (server / "runtime/logs").rmdir()
    failures = [
        result.message
        for result in validate_deployment(server, env_file=env)
        if result.status == "ERROR"
    ]
    assert any(
        "all scoped Data API tokens are distinct" in message for message in failures
    )
    assert any("tls.key" in message for message in failures)
    assert any("logs" in message for message in failures)


def test_explicit_config_override_and_legacy_layout(tmp_path):
    server, env, config, _ = make_deployment(tmp_path)
    selected_config = tmp_path / "selected.yaml"
    config.rename(selected_config)
    assert all(
        result.status == "OK"
        for result in validate_deployment(
            server, env_file=env, config_path=selected_config
        )
    )
    write_env(env, {"SECRETS_FILE": "./secrets/secrets.env"})
    assert any(
        result.status == "ERROR"
        and "migrate SECRETS_FILE/INTERNAL_CLIENT_SECRETS_FILE" in result.message
        for result in validate_deployment(
            server, env_file=env, config_path=selected_config
        )
    )


# 인용 구문 오류가 발생해도 비밀 문자열이 예외 설명을 통해 진단 결과에 들어가지 않아야 한다.
def test_invalid_quoted_secret_is_reported_without_value(tmp_path):
    server, env, _, secrets = make_deployment(tmp_path)
    private_value = r'"private-token-\q"'
    secrets["analysis"].write_text(
        f"DATA_ANALYSIS_TOKEN={private_value}\n", encoding="utf-8"
    )
    results = validate_deployment(server, env_file=env)
    assert any(
        result.status == "ERROR" and "cannot read service secrets" in result.message
        for result in results
    )
    assert private_value not in repr(results)


def test_env_reader_preserves_quoted_paths_and_resolves_relative_to_server(tmp_path):
    path = tmp_path / "quoted.env"
    expected = r"C:\Program Data\AI CCTV\config.yaml"
    path.write_text(
        f"CONFIG_FILE={json.dumps(expected)}\nNAME='camera\\'s config'\n",
        encoding="utf-8",
    )
    assert read_deployment_env(path) == {
        "CONFIG_FILE": expected,
        "NAME": "camera's config",
    }
    assert (
        deployment_path(tmp_path, "config/config.yaml")
        == tmp_path / "config/config.yaml"
    )
