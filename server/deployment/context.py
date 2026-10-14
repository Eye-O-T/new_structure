"""Public, non-secret description of an installed server deployment."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DeploymentContext:
    """Paths and public connection details shared by host-side tools."""

    data_root: Path
    env_file: Path
    config_file: Path
    public_url: str
    rtsp_host: str
    rtsp_port: int
    admin_username: str
    allow_insecure_http: bool = False
