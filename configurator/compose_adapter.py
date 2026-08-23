from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


def default_server_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "server"
    return Path(__file__).resolve().parents[1] / "server"


def default_data_root() -> Path:
    configured = os.getenv("AI_CCTV_DATA_ROOT")
    if configured:
        return Path(configured).expanduser()
    program_data = os.getenv("PROGRAMDATA")
    if program_data:
        return Path(program_data) / "AI_CCTV"
    return Path.home() / ".local" / "share" / "AI_CCTV"


def default_compose_env(server_dir: str | Path | None = None) -> Path:
    configured = os.getenv("AI_CCTV_COMPOSE_ENV_FILE")
    if configured:
        return Path(configured).expanduser().resolve()
    server_root = Path(server_dir or default_server_dir()).resolve()
    development_env = server_root / ".env"
    installed_env = default_data_root() / "config" / "compose.env"
    if getattr(sys, "frozen", False) or installed_env.is_file():
        return installed_env.resolve()
    return development_env


@dataclass(frozen=True)
class Prerequisite:
    ok: bool
    name: str
    message: str


def installation_prerequisites(server_dir: str | Path) -> list[Prerequisite]:
    """Return non-mutating checks required before starting the deployment."""

    server_root = Path(server_dir).resolve()
    results = []
    compose_file = server_root / "compose.yml"
    results.append(
        Prerequisite(
            compose_file.is_file(),
            "Server package",
            str(compose_file)
            if compose_file.is_file()
            else f"compose definition is missing: {compose_file}",
        )
    )
    docker = shutil.which("docker")
    if docker is None:
        results.append(
            Prerequisite(
                False,
                "Docker Desktop",
                "Docker was not found on PATH; install and start Docker Desktop.",
            )
        )
        return results
    try:
        info = subprocess.run(
            [docker, "info"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        compose = subprocess.run(
            [docker, "compose", "version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        results.append(Prerequisite(False, "Docker Desktop", str(exc)))
        return results
    results.append(
        Prerequisite(
            info.returncode == 0,
            "Docker Engine",
            "running"
            if info.returncode == 0
            else "Docker is installed but its engine is not running.",
        )
    )
    results.append(
        Prerequisite(
            compose.returncode == 0,
            "Docker Compose",
            (compose.stdout or compose.stderr).strip()
            or "Docker Compose is unavailable.",
        )
    )
    return results


def _env_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == "'":
            value = value[1:-1].replace("\\'", "'")
        values[key.strip()] = value
    return values


class ComposeAdapter:
    def __init__(
        self, server_dir: str | Path, env_file: str | Path | None = None
    ) -> None:
        self.server_dir = Path(server_dir).resolve()
        self.compose_file = self.server_dir / "compose.yml"
        self.env_file = (
            Path(env_file).expanduser().resolve()
            if env_file is not None
            else default_compose_env(self.server_dir)
        )

    def command(self, *arguments: str) -> list[str]:
        return [
            "docker",
            "compose",
            "--env-file",
            str(self.env_file),
            "-f",
            str(self.compose_file),
            *arguments,
        ]

    def deployment_prerequisites(self) -> list[Prerequisite]:
        results = installation_prerequisites(self.server_dir)
        if not self.env_file.is_file():
            results.append(
                Prerequisite(
                    False,
                    "Compose environment",
                    f"not initialized: {self.env_file}",
                )
            )
            return results
        results.append(Prerequisite(True, "Compose environment", str(self.env_file)))
        values = _env_values(self.env_file)

        def deployment_path(key: str) -> Path | None:
            value = values.get(key)
            if not value:
                return None
            path = Path(value)
            return path if path.is_absolute() else (self.server_dir / path).resolve()

        required_files = {
            "Configuration": deployment_path("CONFIG_FILE"),
            "Data service secrets": deployment_path("DATA_SECRETS_FILE"),
            "External service secrets": deployment_path("EXTERNAL_SECRETS_FILE"),
            "Inference service secrets": deployment_path("INFERENCE_SECRETS_FILE"),
            "Media service secrets": deployment_path("MEDIA_SECRETS_FILE"),
        }
        models_root = deployment_path("MODELS_DIR")
        model_name = values.get("MODEL_FILE")
        required_files["Inference model"] = (
            models_root / model_name if models_root is not None and model_name else None
        )
        certificate_root = deployment_path("CERTS_DIR")
        required_files["TLS certificate"] = (
            certificate_root / "tls.crt" if certificate_root is not None else None
        )
        required_files["TLS private key"] = (
            certificate_root / "tls.key" if certificate_root is not None else None
        )
        for name, path in required_files.items():
            present = path is not None and path.is_file()
            results.append(
                Prerequisite(
                    present,
                    name,
                    str(path) if present else f"missing or not configured: {path or name}",
                )
            )
        return results

    def run(
        self, *arguments: str, capture: bool = False
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            self.command(*arguments),
            cwd=self.server_dir,
            check=False,
            text=True,
            capture_output=capture,
        )

    def start(self) -> int:
        return self.run("up", "-d", "--build", "--wait").returncode

    def stop(self) -> int:
        return self.run("down").returncode

    def restart(self) -> int:
        result = self.run("restart")
        return result.returncode
