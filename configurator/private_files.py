from __future__ import annotations

import os
import subprocess
from pathlib import Path


def restrict_private_file(path: Path) -> None:
    """Restrict a secret file on POSIX and with an explicit Windows DACL."""

    os.chmod(path, 0o600)
    if os.name != "nt":
        return
    try:
        identity = subprocess.run(
            ["whoami"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError("could not identify the Windows installation account") from exc
    if not identity or "\n" in identity or "\r" in identity:
        raise OSError("could not identify the Windows installation account")
    try:
        subprocess.run(
            [
                "icacls",
                str(path),
                "/inheritance:r",
                "/grant:r",
                f"{identity}:(F)",
                "*S-1-5-18:(F)",
                "*S-1-5-32-544:(F)",
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise OSError("could not apply the private Windows file ACL") from exc
