# 소스 배포가 사용할 파일·저장소·서비스별 인증 설정을 점검한다.
# 설정 내용을 출력하지 않고 정상 여부를 보고하며, 구성 오류가 있으면 기동 전에 알린다.


from __future__ import annotations

import hmac
import json
from dataclasses import dataclass
from pathlib import Path


# 진단 항목을 상태·분류·메시지로 전달해 CLI가 여러 실패를 한꺼번에 표시하게 한다.
@dataclass(frozen=True)
class Check:
    status: str
    name: str
    message: str


def _check(ok: bool, message: str) -> Check:
    return Check("OK" if ok else "ERROR", "Deployment", message)


# 설치 도구의 따옴표 규칙으로 env를 읽되 셸 실행이나 환경 변수 확장은 하지 않는다.
def read_deployment_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
            # Windows 경로의 공백·역슬래시는 설치 도우미가 쓴 JSON 인용 규칙으로 복원한다.
            try:
                value = json.loads(value)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid quoted environment value at {path}:{line_number}"
                ) from exc
        elif len(value) >= 2 and value.startswith("'") and value.endswith("'"):
            value = value[1:-1].replace("\\'", "'")
        values[key.strip()] = value
    return values


# 상대 경로는 현재 터미널 위치가 아니라 server 폴더를 기준으로 해석한다.
def deployment_path(server_dir: Path, raw_value: str) -> Path:
    value = Path(raw_value).expanduser()
    return value.resolve() if value.is_absolute() else (server_dir / value).resolve()


def validate_deployment(
    server_dir: Path,
    *,
    env_file: Path | None = None,
    config_path: Path | None = None,
) -> list[Check]:
    """파일·인증값을 읽기 전용으로 검사한다. Docker·Qt·설정 패키지가 필요 없다."""

    server_dir = server_dir.expanduser().resolve()
    env_path = (
        env_file.expanduser().resolve() if env_file is not None else server_dir / ".env"
    )
    read_errors: list[Check] = []
    try:
        deployment_env = read_deployment_env(env_path)
    except (OSError, UnicodeError, ValueError):
        read_errors.append(
            _check(False, f"cannot read deployment environment: {env_path}")
        )
        deployment_env = {}
    config_path = (
        config_path.expanduser().resolve()
        if config_path is not None
        else deployment_path(
            server_dir, deployment_env.get("CONFIG_FILE", "./config/config.yaml")
        )
    )
    secret_defaults = {
        "data": ("DATA_SECRETS_FILE", "./secrets/data.env"),
        "external": ("EXTERNAL_SECRETS_FILE", "./secrets/external.env"),
        "preprocessing": ("PREPROCESSING_SECRETS_FILE", "./secrets/preprocessing.env"),
        "media": ("MEDIA_SECRETS_FILE", "./secrets/media.env"),
        "analysis": ("ANALYSIS_SECRETS_FILE", "./secrets/analysis.env"),
    }
    secret_paths = {
        service: deployment_path(server_dir, deployment_env.get(variable, default))
        for service, (variable, default) in secret_defaults.items()
    }
    secrets_paths = tuple(secret_paths.values())
    tls_dir = deployment_path(
        server_dir,
        deployment_env.get(
            "CERTS_DIR",
            deployment_env.get("TLS_DIR", "./runtime/certificates"),
        ),
    )
    required_files = (
        env_path,
        server_dir / "compose.yml",
        config_path,
        *secrets_paths,
        server_dir / "services" / "nginx" / "nginx.conf",
        server_dir / "services" / "mediamtx" / "mediamtx.yml",
        tls_dir / "tls.crt",
        tls_dir / "tls.key",
    )
    required_directories = tuple(
        deployment_path(server_dir, deployment_env.get(variable, default))
        for variable, default in (
            ("DATABASE_DIR", "./runtime/database"),
            ("RECORDINGS_DIR", "./runtime/recordings"),
            ("RECOVERED_DIR", "./runtime/recovered"),
            ("SNAPSHOTS_DIR", "./runtime/snapshots"),
            ("MODELS_DIR", "./runtime/models"),
            ("LOGS_DIR", "./runtime/logs"),
        )
    )

    configured_split_variables = all(
        variable in deployment_env for variable, _default in secret_defaults.values()
    )
    legacy_layout = (
        bool(
            deployment_env.get("SECRETS_FILE")
            or deployment_env.get("INTERNAL_CLIENT_SECRETS_FILE")
        )
        and not configured_split_variables
    )
    checks = [
        *read_errors,
        _check(
            not legacy_layout,
            (
                "legacy SECRETS_FILE/INTERNAL_CLIENT_SECRETS_FILE deployment is "
                "unsupported; migrate SECRETS_FILE/INTERNAL_CLIENT_SECRETS_FILE "
                "before validating this deployment"
                if legacy_layout
                else "split service secret files are configured"
            ),
        ),
        _check(
            len(set(secrets_paths)) == len(secrets_paths),
            "Data, External, Preprocessing, Media, and Analysis use distinct secret files",
        ),
    ]
    checks.extend(_check(path.is_file(), f"file: {path}") for path in required_files)
    checks.extend(
        _check(path.is_dir(), f"directory: {path}") for path in required_directories
    )

    secret_values: dict[str, dict[str, str]] = {}
    for service, secrets_path in secret_paths.items():
        if not secrets_path.is_file():
            secret_values[service] = {}
            continue
        try:
            secret_text = secrets_path.read_text(encoding="utf-8")
            checks.append(
                _check(
                    "replace-with" not in secret_text
                    and "<argon2id" not in secret_text,
                    f"secret file does not contain placeholders: {secrets_path}",
                )
            )
            secret_values[service] = read_deployment_env(secrets_path)
        except (OSError, UnicodeError, ValueError):
            checks.append(_check(False, f"cannot read service secrets: {secrets_path}"))
            secret_values[service] = {}

    # 파일 누락은 앞에서 보고했다. 필요한 파일이 모두 있을 때만 역할별 값의 계약을 대조한다.
    if all(path.is_file() for path in secrets_paths):
        allowed_keys = {
            "data": {
                "DATA_EXTERNAL_TOKEN",
                "DATA_INFERENCE_TOKEN",
                "DATA_MEDIA_TOKEN",
                "DATA_RECOVERY_TOKEN",
                "DATA_IDENTITY_TOKEN",
                "DATA_ANALYSIS_TOKEN",
                "EDGE_AUTH_TOKENS_JSON",
                "INITIAL_ADMIN_USERNAME",
                "INITIAL_ADMIN_PASSWORD_HASH",
            },
            "external": {
                "DATA_EXTERNAL_TOKEN",
                "JWT_SECRET",
                "MEDIA_READ_USERNAME",
                "MEDIA_READ_PASSWORD",
                "MEDIA_PUBLISH_CREDENTIALS_JSON",
            },
            "preprocessing": {
                "DATA_INFERENCE_TOKEN",
                "DATA_IDENTITY_TOKEN",
                "MEDIA_READ_USERNAME",
                "MEDIA_READ_PASSWORD",
            },
            "media": {"DATA_MEDIA_TOKEN"},
            "analysis": {"DATA_ANALYSIS_TOKEN"},
        }
        for service, values in secret_values.items():
            forbidden = sorted(set(values) - allowed_keys[service])
            checks.append(
                _check(
                    not forbidden,
                    f"{service}.env contains only its allowed secret keys"
                    + (f" (forbidden: {', '.join(forbidden)})" if forbidden else ""),
                )
            )
        scoped_keys = {
            "external": "DATA_EXTERNAL_TOKEN",
            "inference": "DATA_INFERENCE_TOKEN",
            "media": "DATA_MEDIA_TOKEN",
            "recovery": "DATA_RECOVERY_TOKEN",
            "identity": "DATA_IDENTITY_TOKEN",
            "analysis": "DATA_ANALYSIS_TOKEN",
        }
        data_tokens: dict[str, str] = {}
        for scope, key in scoped_keys.items():
            value = secret_values["data"].get(key, "")
            data_tokens[scope] = value
            checks.append(
                _check(
                    len(value) >= 32,
                    f"data.env contains a 32+ character {key}",
                )
            )
        consumers = {
            "external": ("external",),
            "preprocessing": ("inference", "identity"),
            "media": ("media",),
            "analysis": ("analysis",),
        }
        for service, scopes in consumers.items():
            for scope in scopes:
                key = scoped_keys[scope]
                value = secret_values[service].get(key, "")
                checks.append(
                    _check(
                        len(value) >= 32,
                        f"{service}.env contains a 32+ character {key}",
                    )
                )
                checks.append(
                    _check(
                        bool(value)
                        and bool(data_tokens[scope])
                        and hmac.compare_digest(
                            value.encode("utf-8"), data_tokens[scope].encode("utf-8")
                        ),
                        f"{key} matches between data.env and {service}.env",
                    )
                )
        # 소비자와의 값 일치만으로는 부족하다. 역할 간 토큰 재사용도 별도로 거부한다.
        configured_tokens = [value for value in data_tokens.values() if value]
        checks.append(
            _check(
                len(configured_tokens) == len(scoped_keys)
                and len(set(configured_tokens)) == len(configured_tokens),
                "all scoped Data API tokens are distinct",
            )
        )
        jwt_secret = secret_values["external"].get("JWT_SECRET", "")
        checks.append(
            _check(
                len(jwt_secret.encode("utf-8")) >= 32,
                "external.env contains a 32+ byte JWT_SECRET",
            )
        )
        external_read_username = secret_values["external"].get(
            "MEDIA_READ_USERNAME", ""
        )
        preprocessing_read_username = secret_values["preprocessing"].get(
            "MEDIA_READ_USERNAME", ""
        )
        external_read_password = secret_values["external"].get(
            "MEDIA_READ_PASSWORD", ""
        )
        preprocessing_read_password = secret_values["preprocessing"].get(
            "MEDIA_READ_PASSWORD", ""
        )
        checks.extend(
            (
                _check(
                    bool(external_read_username),
                    "external.env contains MEDIA_READ_USERNAME",
                ),
                _check(
                    bool(preprocessing_read_username),
                    "preprocessing.env contains MEDIA_READ_USERNAME",
                ),
                _check(
                    bool(external_read_username)
                    and bool(preprocessing_read_username)
                    and hmac.compare_digest(
                        external_read_username.encode("utf-8"),
                        preprocessing_read_username.encode("utf-8"),
                    ),
                    "MEDIA_READ_USERNAME matches between external.env and preprocessing.env",
                ),
                _check(
                    len(external_read_password) >= 32,
                    "external.env contains a 32+ character MEDIA_READ_PASSWORD",
                ),
                _check(
                    len(preprocessing_read_password) >= 32,
                    "preprocessing.env contains a 32+ character MEDIA_READ_PASSWORD",
                ),
                _check(
                    bool(external_read_password)
                    and bool(preprocessing_read_password)
                    and hmac.compare_digest(
                        external_read_password.encode("utf-8"),
                        preprocessing_read_password.encode("utf-8"),
                    ),
                    "MEDIA_READ_PASSWORD matches between external.env and preprocessing.env",
                ),
            )
        )

    return checks
