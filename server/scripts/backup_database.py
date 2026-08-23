"""Request a consistent SQLite backup from the running Data Service."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import textwrap
from pathlib import Path


CONTAINER_SCRIPT = textwrap.dedent(
    """
    import json
    import os
    import sys
    import urllib.request

    class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):
            return None

    values = json.load(sys.stdin)
    body = json.dumps({"filename": values.get("filename")}).encode("utf-8")
    token = os.environ["DATA_EXTERNAL_TOKEN"]
    request = urllib.request.Request(
        "http://127.0.0.1:8000/internal/v1/backup",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Internal-Token": token,
        },
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}), NoRedirectHandler()
    )
    with opener.open(request, timeout=30) as response:
        result = json.load(response)
    print(json.dumps(result))
    """
).strip()


def parse_args() -> argparse.Namespace:
    server_dir = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument("--filename")
    parser.add_argument("--server-dir", type=Path, default=server_dir)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.filename is not None:
        if not args.filename.endswith(".db") or any(
            value in args.filename for value in ("/", "\\", "..")
        ):
            raise SystemExit("--filename must be a simple .db filename")

    docker = shutil.which("docker")
    if docker is None:
        raise SystemExit("Docker CLI was not found on PATH")
    server_dir = args.server_dir.expanduser().resolve()
    command = [
        docker,
        "compose",
        "--env-file",
        str(server_dir / ".env"),
        "-f",
        str(server_dir / "compose.yml"),
        "exec",
        "-T",
        "data",
        "python",
        "-c",
        CONTAINER_SCRIPT,
    ]
    result = subprocess.run(
        command,
        input=json.dumps({"filename": args.filename}),
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or "database backup failed")
    print(f"[OK] database backup created: {result.stdout.strip()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
