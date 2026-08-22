from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ai_cctv_core.config import load_config

from .compose_adapter import ComposeAdapter


def _deployment_env(path: Path) -> dict[str, str]:
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


def _compose_rows(raw: str) -> list[dict]:
    text = raw.strip()
    if not text:
        return []
    try:
        decoded = json.loads(text)
        return decoded if isinstance(decoded, list) else [decoded]
    except json.JSONDecodeError:
        rows = []
        for line in text.splitlines():
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                rows.append(value)
        return rows


@dataclass(frozen=True)
class Check:
    status: str
    name: str
    message: str


def checks(server_dir: Path, config_path: Path) -> list[Check]:
    results: list[Check] = []
    docker = shutil.which("docker")
    if not docker:
        return [
            Check(
                "ERROR",
                "Docker Engine",
                "Docker was not found. Install/start Docker Desktop and retry.",
            )
        ]
    info = subprocess.run(
        [docker, "info"], capture_output=True, text=True, timeout=20, check=False
    )
    results.append(
        Check(
            "OK" if info.returncode == 0 else "ERROR",
            "Docker Engine",
            "running"
            if info.returncode == 0
            else "Docker is installed but not running",
        )
    )
    version = subprocess.run(
        [docker, "compose", "version"],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    results.append(
        Check(
            "OK" if version.returncode == 0 else "ERROR",
            "Docker Compose",
            version.stdout.strip() or version.stderr.strip(),
        )
    )
    try:
        config = load_config(config_path)
        results.append(Check("OK", "Configuration", f"schema={config.schema_version}"))
        environment = _deployment_env(server_dir / ".env")
        runtime_uid = environment.get("AI_CCTV_UID")
        results.append(
            Check(
                "WARN" if runtime_uid == "0" else "OK",
                "Container runtime user",
                "UID 0 selected; configure a mapped non-root AI_CCTV_RUNTIME_UID"
                if runtime_uid == "0"
                else f"UID {runtime_uid or 'image default'}",
            )
        )
        recording = Path(environment.get("RECORDINGS_DIR", config.recording.root))
        if not recording.is_absolute():
            recording = (server_dir / recording).resolve()
        writable = recording.exists() and os_access_write(recording)
        results.append(
            Check(
                "OK" if writable else "ERROR",
                "Recording storage",
                str(recording),
            )
        )
        models_root = Path(environment.get("MODELS_DIR", server_dir / "runtime/models"))
        if not models_root.is_absolute():
            models_root = (server_dir / models_root).resolve()
        model = models_root / environment.get(
            "MODEL_FILE", Path(config.inference.model_path).name
        )
        results.append(
            Check(
                "OK" if model.is_file() else "WARN",
                "Inference model",
                str(model)
                if model.is_file()
                else "missing; service will use non-inference mode",
            )
        )
    except Exception as exc:
        results.append(Check("ERROR", "Configuration", str(exc)))
    adapter = ComposeAdapter(server_dir)
    if adapter.env_file.exists() and adapter.compose_file.exists():
        compose = adapter.run("config", "--quiet", capture=True)
        results.append(
            Check(
                "OK" if compose.returncode == 0 else "ERROR",
                "Compose configuration",
                "valid" if compose.returncode == 0 else compose.stderr.strip(),
            )
        )
        if compose.returncode == 0:
            ps = adapter.run("ps", "--format", "json", capture=True)
            rows = _compose_rows(ps.stdout) if ps.returncode == 0 else []
            by_service = {
                str(row.get("Service") or row.get("Name")): row for row in rows
            }
            for service in ("data", "external", "inference", "mediamtx", "nginx"):
                row = by_service.get(service)
                state = str((row or {}).get("State", "stopped")).lower()
                health = str((row or {}).get("Health", "")).lower()
                ok = state == "running" and health in {"", "healthy"}
                starting = state == "running" and health == "starting"
                results.append(
                    Check(
                        "OK" if ok else "WARN" if starting else "ERROR",
                        f"Service {service}",
                        f"state={state}, health={health or 'n/a'}",
                    )
                )

            if by_service.get("inference") is not None:
                camera_probe = adapter.run(
                    "exec",
                    "-T",
                    "inference",
                    "python",
                    "-c",
                    (
                        "import urllib.request;"
                        "print(urllib.request.urlopen("
                        "'http://127.0.0.1:8000/internal/v1/status',timeout=3"
                        ").read().decode())"
                    ),
                    capture=True,
                )
                if camera_probe.returncode == 0:
                    try:
                        workers = json.loads(camera_probe.stdout).get("workers", {})
                    except (AttributeError, json.JSONDecodeError):
                        workers = {}
                    for camera_id, worker in sorted(workers.items()):
                        camera_state = str(worker.get("state", "unknown"))
                        results.append(
                            Check(
                                "OK" if camera_state == "online" else "WARN",
                                f"Camera {camera_id}",
                                camera_state,
                            )
                        )
    else:
        results.append(
            Check("ERROR", "Compose files", "server/.env or compose.yml missing")
        )
    return results


def os_access_write(path: Path) -> bool:
    import os

    return os.access(path, os.W_OK)
