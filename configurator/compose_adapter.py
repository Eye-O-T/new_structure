from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def default_server_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "server"
    return Path(__file__).resolve().parents[1] / "server"


class ComposeAdapter:
    def __init__(self, server_dir: str | Path):
        self.server_dir = Path(server_dir).resolve()
        self.compose_file = self.server_dir / "compose.yml"
        self.env_file = self.server_dir / ".env"

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
