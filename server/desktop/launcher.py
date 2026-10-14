"""Launch desktop tools as independent processes in source and packaged runs."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Sequence

from server.deployment.context import DeploymentContext


_EXECUTABLES = {
    "server_manager": "AI_CCTV_Server_Manager.exe",
    "admin": "AI_CCTV_Admin.exe",
}
_MODULES = {
    "server_manager": "server.desktop.server_manager",
    "admin": "server.desktop.admin",
}


def server_manager_arguments(server_dir: Path, data_root: Path) -> tuple[str, ...]:
    """Build the Server Manager arguments for one installed deployment."""

    return ("--server-dir", str(server_dir), "--data-root", str(data_root))


def admin_arguments(deployment: DeploymentContext) -> tuple[str, ...]:
    """Build non-secret Admin connection arguments from deployment metadata."""

    arguments = [
        "--server-url",
        deployment.public_url,
        "--username",
        deployment.admin_username,
        "--storage-path",
        str(deployment.data_root),
        "--rtsp-host",
        deployment.rtsp_host,
        "--rtsp-port",
        str(deployment.rtsp_port),
    ]
    certificate = deployment.data_root / "certs" / "tls.crt"
    if certificate.is_file():
        arguments.extend(("--ca-file", str(certificate)))
    if deployment.allow_insecure_http:
        arguments.append("--allow-insecure-http")
    return tuple(arguments)


def desktop_command(tool: str, arguments: Sequence[str] = ()) -> list[str]:
    """Return a command that starts a desktop tool outside the caller process."""

    if tool not in _EXECUTABLES:
        raise ValueError(f"unknown desktop tool: {tool}")
    if getattr(sys, "frozen", False):
        executable = Path(sys.executable).with_name(_EXECUTABLES[tool])
        return [str(executable), *arguments]
    return [sys.executable, "-m", _MODULES[tool], *arguments]


def launch_desktop(tool: str, arguments: Sequence[str] = ()) -> subprocess.Popen:
    """Start a desktop tool without inheriting the installer's lifetime."""

    return subprocess.Popen(desktop_command(tool, arguments))
