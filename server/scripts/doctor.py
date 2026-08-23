"""Preflight checks for the central Docker Compose deployment."""

from __future__ import annotations

import argparse
import hmac
import json
import shutil
import subprocess
from pathlib import Path


def parse_args() -> argparse.Namespace:
    server_dir = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--server-dir", type=Path, default=server_dir)
    parser.add_argument("--skip-compose", action="store_true")
    return parser.parse_args()


def report(ok: bool, message: str) -> bool:
    print(f"[{'OK' if ok else 'ERROR'}] {message}")
    return ok


def read_deployment_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
            # Configurator emits JSON-quoted values when Windows paths contain
            # spaces or backslashes. Decode them instead of retaining escapes.
            value = json.loads(value)
        elif len(value) >= 2 and value.startswith("'") and value.endswith("'"):
            value = value[1:-1]
        values[key.strip()] = value
    return values


def deployment_path(server_dir: Path, raw_value: str) -> Path:
    value = Path(raw_value).expanduser()
    return value.resolve() if value.is_absolute() else (server_dir / value).resolve()


def main() -> int:
    args = parse_args()
    server_dir = args.server_dir.expanduser().resolve()
    env_path = server_dir / ".env"
    deployment_env = read_deployment_env(env_path)
    config_path = deployment_path(
        server_dir, deployment_env.get("CONFIG_FILE", "./config/config.yaml")
    )
    secret_defaults = {
        "data": ("DATA_SECRETS_FILE", "./secrets/data.env"),
        "external": ("EXTERNAL_SECRETS_FILE", "./secrets/external.env"),
        "inference": ("INFERENCE_SECRETS_FILE", "./secrets/inference.env"),
        "media": ("MEDIA_SECRETS_FILE", "./secrets/media.env"),
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
        server_dir / "nginx" / "nginx.conf",
        server_dir / "mediamtx" / "mediamtx.yml",
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
        report(
            not legacy_layout,
            (
                "legacy SECRETS_FILE/INTERNAL_CLIENT_SECRETS_FILE deployment is "
                "unsupported; migrate SECRETS_FILE/INTERNAL_CLIENT_SECRETS_FILE "
                "with generate_secrets.py"
                if legacy_layout
                else "split service secret files are configured"
            ),
        ),
        report(
            len(set(secrets_paths)) == len(secrets_paths),
            "Data, External, Inference, and Media use distinct secret files",
        ),
    ]
    checks.extend(report(path.is_file(), f"file: {path}") for path in required_files)
    checks.extend(
        report(path.is_dir(), f"directory: {path}") for path in required_directories
    )

    for secrets_path in secrets_paths:
        if secrets_path.is_file():
            secret_text = secrets_path.read_text(encoding="utf-8")
            checks.append(
                report(
                    "replace-with" not in secret_text
                    and "<argon2id" not in secret_text,
                    f"secret file does not contain placeholders: {secrets_path}",
                )
            )

    if all(path.is_file() for path in secrets_paths):
        secret_values = {
            service: read_deployment_env(path) for service, path in secret_paths.items()
        }
        allowed_keys = {
            "data": {
                "DATA_EXTERNAL_TOKEN",
                "DATA_INFERENCE_TOKEN",
                "DATA_MEDIA_TOKEN",
                "DATA_RECOVERY_TOKEN",
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
            "inference": {
                "DATA_INFERENCE_TOKEN",
                "MEDIA_READ_USERNAME",
                "MEDIA_READ_PASSWORD",
            },
            "media": {"DATA_MEDIA_TOKEN"},
        }
        for service, values in secret_values.items():
            forbidden = sorted(set(values) - allowed_keys[service])
            checks.append(
                report(
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
        }
        data_tokens: dict[str, str] = {}
        for scope, key in scoped_keys.items():
            value = secret_values["data"].get(key, "")
            data_tokens[scope] = value
            checks.append(
                report(
                    len(value) >= 32,
                    f"data.env contains a 32+ character {key}",
                )
            )
        for service in ("external", "inference", "media"):
            key = scoped_keys[service]
            consumer_value = secret_values[service].get(key, "")
            checks.append(
                report(
                    len(consumer_value) >= 32,
                    f"{service}.env contains a 32+ character {key}",
                )
            )
            checks.append(
                report(
                    bool(consumer_value)
                    and bool(data_tokens[service])
                    and hmac.compare_digest(consumer_value, data_tokens[service]),
                    f"{key} matches between data.env and {service}.env",
                )
            )
        configured_tokens = [value for value in data_tokens.values() if value]
        checks.append(
            report(
                len(configured_tokens) == len(scoped_keys)
                and len(set(configured_tokens)) == len(configured_tokens),
                "all scoped Data API tokens are distinct",
            )
        )
        jwt_secret = secret_values["external"].get("JWT_SECRET", "")
        checks.append(
            report(
                len(jwt_secret.encode("utf-8")) >= 32,
                "external.env contains a 32+ byte JWT_SECRET",
            )
        )
        external_read_username = secret_values["external"].get(
            "MEDIA_READ_USERNAME", ""
        )
        inference_read_username = secret_values["inference"].get(
            "MEDIA_READ_USERNAME", ""
        )
        external_read_password = secret_values["external"].get(
            "MEDIA_READ_PASSWORD", ""
        )
        inference_read_password = secret_values["inference"].get(
            "MEDIA_READ_PASSWORD", ""
        )
        checks.extend(
            (
                report(
                    bool(external_read_username),
                    "external.env contains MEDIA_READ_USERNAME",
                ),
                report(
                    bool(inference_read_username),
                    "inference.env contains MEDIA_READ_USERNAME",
                ),
                report(
                    bool(external_read_username)
                    and bool(inference_read_username)
                    and hmac.compare_digest(
                        external_read_username.encode("utf-8"),
                        inference_read_username.encode("utf-8"),
                    ),
                    "MEDIA_READ_USERNAME matches between external.env and inference.env",
                ),
                report(
                    len(external_read_password) >= 32,
                    "external.env contains a 32+ character MEDIA_READ_PASSWORD",
                ),
                report(
                    len(inference_read_password) >= 32,
                    "inference.env contains a 32+ character MEDIA_READ_PASSWORD",
                ),
                report(
                    bool(external_read_password)
                    and bool(inference_read_password)
                    and hmac.compare_digest(
                        external_read_password.encode("utf-8"),
                        inference_read_password.encode("utf-8"),
                    ),
                    "MEDIA_READ_PASSWORD matches between external.env and inference.env",
                ),
            )
        )

    if not args.skip_compose:
        docker = shutil.which("docker")
        checks.append(report(docker is not None, "Docker CLI is available"))
        if docker is not None and env_path.is_file():
            command = [
                docker,
                "compose",
                "--env-file",
                str(env_path),
                "-f",
                str(server_dir / "compose.yml"),
                "config",
                "--quiet",
            ]
            result = subprocess.run(command, check=False)
            checks.append(
                report(result.returncode == 0, "Compose configuration is valid")
            )

    return 0 if all(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
