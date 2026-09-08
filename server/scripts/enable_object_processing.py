# 기존 감지·인물 연결 설정을 현재 Preprocessing·Analysis 구성으로 이관한다.
# 운영 토큰을 새로 바꾸지 않고 일치 여부를 확인하며 누락된 역할의 토큰만 보충한다.


import argparse
import secrets
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server.scripts.generate_secrets import atomic_write, single_quote  # noqa: E402
from server.scripts.doctor import deployment_path, read_deployment_env  # noqa: E402


PLUGIN_DEFAULTS = {
    "DETECTION_PLUGIN": "server.services.preprocessing.processors.detection.yolo:YoloTracker",
    "IDENTITY_PLUGIN": "server.services.preprocessing.processors.identity:IdentityBlackBox",
    "ANALYSIS_PLUGIN": "server.services.analysis.processors:MetadataBlackBox",
}
OLD_PLUGIN_DEFAULTS = {
    "DETECTION_PLUGIN": {
        "app.pipeline:YoloTracker",
        "server.services.inference.app.pipeline:YoloTracker",
    },
    "IDENTITY_PLUGIN": {
        "app.plugins:IdentityBlackBox",
        "server.services.object_processing.app.plugins:IdentityBlackBox",
    },
    "ANALYSIS_PLUGIN": {
        "app.plugins:MetadataBlackBox",
        "server.services.object_processing.app.plugins:MetadataBlackBox",
    },
}


def _update_env(
    text: str, updates: dict[str, str], remove: set[str] | None = None
) -> str:
    """대상 키만 한 번 교체하고 나머지 설정·주석은 보존한다."""
    lines = []
    remaining = dict(updates)
    for line in text.splitlines():
        stripped = line.strip()
        key = stripped.split("=", 1)[0].strip() if "=" in stripped else None
        if not stripped or stripped.startswith("#"):
            lines.append(line)
        elif key in (remove or set()):
            continue
        elif key in updates:
            if key in remaining:
                lines.append(f"{key}={single_quote(remaining.pop(key))}")
        else:
            lines.append(line)
    lines.extend(f"{key}={single_quote(value)}" for key, value in remaining.items())
    return "\n".join(lines).rstrip() + "\n"


def _source(path: Path, allowed_keys: set[str]) -> dict[str, str]:
    if path.exists() and not path.is_file():
        raise ValueError("A service secret path is not a regular file")
    values = read_deployment_env(path)
    if set(values) - allowed_keys:
        raise ValueError("A service secret file contains unrelated keys")
    return values


# 여러 파일에 같은 역할의 토큰이 있으면 값이 모두 같아야 한다. 충돌을 임의 선택하지 않는다.
def _agree(key: str, candidates: list[dict[str, str]]) -> str | None:
    values = {candidate[key] for candidate in candidates if candidate.get(key)}
    if len(values) > 1:
        raise ValueError(f"{key} is inconsistent between existing secret files")
    return next(iter(values), None)


# 모든 입력·경로·토큰을 검증한 다음 파일별로 교체한다. 중간 중단 뒤 재실행해도 토큰을 재사용한다.
def enable(server_dir: Path, env_file: Path) -> None:
    server_dir, env_file = server_dir.resolve(), env_file.resolve()
    values = read_deployment_env(env_file)
    data_path = deployment_path(
        server_dir, values.get("DATA_SECRETS_FILE", "./secrets/data.env")
    )
    data_values = read_deployment_env(data_path)
    if not env_file.is_file() or not data_path.is_file():
        raise ValueError("Initialize a split-secret deployment first")
    if not all(
        data_values.get(f"DATA_{scope}_TOKEN")
        for scope in ("EXTERNAL", "INFERENCE", "MEDIA", "RECOVERY")
    ):
        raise ValueError(
            "Migrate legacy combined secrets before enabling object workers"
        )

    def secret_path(variable: str, filename: str) -> Path:
        return (
            deployment_path(server_dir, values[variable])
            if values.get(variable)
            else data_path.with_name(filename)
        )

    preprocessing_path = secret_path("PREPROCESSING_SECRETS_FILE", "preprocessing.env")
    analysis_path = secret_path("ANALYSIS_SECRETS_FILE", "analysis.env")
    inference_path = secret_path("INFERENCE_SECRETS_FILE", "inference.env")
    identity_path = secret_path("IDENTITY_SECRETS_FILE", "identity.env")
    protected_paths = {data_path, env_file, inference_path, identity_path}
    protected_paths.update(
        deployment_path(server_dir, raw)
        for key, raw in values.items()
        if key.endswith("_SECRETS_FILE")
        and key not in {"PREPROCESSING_SECRETS_FILE", "ANALYSIS_SECRETS_FILE"}
    )
    protected_paths.update(
        secret_path(variable, filename)
        for variable, filename in (
            ("EXTERNAL_SECRETS_FILE", "external.env"),
            ("MEDIA_SECRETS_FILE", "media.env"),
        )
    )
    if preprocessing_path == analysis_path or any(
        path in protected_paths for path in (preprocessing_path, analysis_path)
    ):
        raise ValueError(
            "New service secrets must use distinct files; legacy files are preserved"
        )
    media_keys = {"MEDIA_READ_USERNAME", "MEDIA_READ_PASSWORD"}
    inference_keys = {"DATA_INFERENCE_TOKEN", *media_keys}
    preprocessing = _source(
        preprocessing_path, {*inference_keys, "DATA_IDENTITY_TOKEN"}
    )
    inference = _source(inference_path, inference_keys)
    identity = _source(identity_path, {"DATA_IDENTITY_TOKEN"})
    analysis = _source(analysis_path, {"DATA_ANALYSIS_TOKEN"})
    for variable, path in (
        ("INFERENCE_SECRETS_FILE", inference_path),
        ("IDENTITY_SECRETS_FILE", identity_path),
    ):
        if values.get(variable) and not path.is_file():
            raise ValueError(f"Configured {variable} does not exist")

    # Data 토큰과 기존 작업자 토큰의 일치를 확인하고, 없는 식별·분석 토큰만 보충한다.
    _agree("DATA_INFERENCE_TOKEN", [data_values, preprocessing, inference])
    added_tokens = {}
    for token_key, candidates in (
        ("DATA_IDENTITY_TOKEN", [data_values, preprocessing, identity]),
        ("DATA_ANALYSIS_TOKEN", [data_values, analysis]),
    ):
        token = _agree(token_key, candidates) or secrets.token_urlsafe(48)
        if not data_values.get(token_key):
            added_tokens[token_key] = token
        data_values[token_key] = token
    service_tokens = [
        data_values[f"DATA_{scope}_TOKEN"]
        for scope in (
            "EXTERNAL",
            "INFERENCE",
            "MEDIA",
            "RECOVERY",
            "IDENTITY",
            "ANALYSIS",
        )
    ]
    if any(
        len(token) < 32
        or any(character.isspace() or character == "\x00" for character in token)
        for token in service_tokens
    ):
        raise ValueError(
            "Service tokens must contain at least 32 non-whitespace characters"
        )
    if len(set(service_tokens)) != len(service_tokens):
        raise ValueError("Service tokens must be distinct")

    media = {}
    external = read_deployment_env(secret_path("EXTERNAL_SECRETS_FILE", "external.env"))
    for key in media_keys:
        value = _agree(key, [preprocessing, inference])
        if not value:
            raise ValueError(
                f"Existing inference/preprocessing secrets must provide {key}"
            )
        _agree(key, [{key: value}, external])
        if any(character in value for character in "\x00\r\n") or (
            key == "MEDIA_READ_PASSWORD" and len(value) < 32
        ):
            raise ValueError(f"{key} is invalid")
        media[key] = value
    preprocessing_values = {
        "DATA_INFERENCE_TOKEN": data_values["DATA_INFERENCE_TOKEN"],
        "DATA_IDENTITY_TOKEN": data_values["DATA_IDENTITY_TOKEN"],
        "MEDIA_READ_USERNAME": media["MEDIA_READ_USERNAME"],
        "MEDIA_READ_PASSWORD": media["MEDIA_READ_PASSWORD"],
    }
    outputs = {
        preprocessing_path: _update_env(
            preprocessing_path.read_text(encoding="utf-8")
            if preprocessing_path.is_file()
            else "",
            preprocessing_values,
        ),
        analysis_path: _update_env(
            analysis_path.read_text(encoding="utf-8")
            if analysis_path.is_file()
            else "",
            {"DATA_ANALYSIS_TOKEN": data_values["DATA_ANALYSIS_TOKEN"]},
        ),
    }
    env_updates = {
        "PREPROCESSING_SECRETS_FILE": str(preprocessing_path),
        "ANALYSIS_SECRETS_FILE": str(analysis_path),
    }
    for key, default in PLUGIN_DEFAULTS.items():
        if not values.get(key) or values[key] in OLD_PLUGIN_DEFAULTS[key]:
            env_updates[key] = default
    data_text = _update_env(data_path.read_text(encoding="utf-8"), added_tokens)
    env_text = _update_env(
        env_file.read_text(encoding="utf-8"),
        env_updates,
        {"INFERENCE_SECRETS_FILE", "IDENTITY_SECRETS_FILE"},
    )
    # 전체 검증 후 파일별로 원자 교체한다. 중단 후 재실행해도 기존 토큰은 유지한다.
    for path, content in outputs.items():
        atomic_write(path, content)
    atomic_write(data_path, data_text)
    atomic_write(env_file, env_text)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-dir", type=Path, default=PROJECT_ROOT / "server")
    parser.add_argument("--env-file", type=Path, required=True)
    args = parser.parse_args()
    enable(args.server_dir.resolve(), args.env_file.resolve())
    print(
        "Preprocessing/analysis settings migrated; recreate the Compose services to apply."
    )


if __name__ == "__main__":
    main()
