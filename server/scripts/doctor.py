"""Preflight checks for the central Docker Compose deployment."""

from __future__ import annotations

import argparse
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
    secrets_path = deployment_path(
        server_dir, deployment_env.get("SECRETS_FILE", "./secrets/secrets.env")
    )
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
        secrets_path,
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

    checks = [report(path.is_file(), f"file: {path}") for path in required_files]
    checks.extend(
        report(path.is_dir(), f"directory: {path}") for path in required_directories
    )

    if secrets_path.is_file():
        secret_text = secrets_path.read_text(encoding="utf-8")
        checks.append(
            report(
                "replace-with" not in secret_text and "<argon2id" not in secret_text,
                "secret file does not contain documented placeholders",
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
