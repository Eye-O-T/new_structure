# Data 컨테이너의 백업 API로 실행 중인 SQLite의 일관된 복사본을 만든다.
# 개발·설치 배포가 공유하는 Compose 경로 선택을 사용한다.


from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import textwrap
from pathlib import Path

from server.operations.compose import ComposeAdapter


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


# 저장소 소스 또는 설치본에서 Compose 디렉터리를 찾고 필요하면 env 경로를 재정의한다.
def default_server_dir() -> Path:
    # 저장소 원본과 설치본의 server/scripts 배치에서 compose.yml을 찾아 선택한다.
    candidates = (
        Path(__file__).resolve().parents[3],
        Path(__file__).resolve().parents[1],
        Path(__file__).resolve().parents[2],
    )
    for candidate in candidates:
        if (candidate / "compose.yml").is_file():
            return candidate
    return candidates[0]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--filename")
    parser.add_argument("--server-dir", type=Path, default=default_server_dir())
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Compose env file (defaults to the installed or development configuration)",
    )
    return parser.parse_args()


# 단순 .db 파일명을 검증한 뒤 Data 컨테이너의 내부 인증값으로 백업 API를 호출한다.
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
    deployment = ComposeAdapter(server_dir, args.env_file)
    command = [
        docker,
        *deployment.command(
            "exec",
            "-T",
            "data",
            "python",
            "-c",
            CONTAINER_SCRIPT,
        ),
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
