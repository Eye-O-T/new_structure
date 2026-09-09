# 소스 배포의 첫 관리자를 생성한다. 비밀번호를 숨김 입력으로 받아 컨테이너 안에서 해시한다.
# 명령 인자에 비밀번호를 넣지 않고 표준 입력으로 전달하여 프로세스 목록 노출을 줄인다.


from __future__ import annotations

import argparse
import getpass
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

    import httpx

    from app.security.passwords import hash_password

    values = json.load(sys.stdin)
    token = os.environ["DATA_EXTERNAL_TOKEN"]
    with httpx.Client(trust_env=False, timeout=10) as client:
        response = client.post(
            "http://nginx:8080/internal/data/v1/users",
            headers={"X-Internal-Token": token},
            json={
                "username": values["username"],
                "password_hash": hash_password(values["password"]),
                "role": "admin",
                "is_active": True,
            },
        )
    response.raise_for_status()
    user = response.json()
    print(json.dumps({
        "id": user.get("id"),
        "username": user.get("username"),
        "role": user.get("role"),
        "is_active": user.get("is_active"),
    }))
    """
).strip()


# 관리자 이름과 Compose 배포 디렉터리를 받으며 비밀번호는 명령 인자로 받지 않는다.
def parse_args() -> argparse.Namespace:
    server_dir = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser()
    parser.add_argument("--username", default="admin")
    parser.add_argument("--server-dir", type=Path, default=server_dir)
    return parser.parse_args()


# 숨김 입력·확인을 마친 비밀번호를 표준 입력으로 전달하고 External 컨테이너에서 해시한다.
def main() -> int:
    args = parse_args()
    if not args.username or len(args.username) > 128:
        raise SystemExit("username must contain between 1 and 128 characters")

    password = getpass.getpass("New administrator password: ")
    confirmation = getpass.getpass("Confirm administrator password: ")
    if password != confirmation:
        raise SystemExit("password confirmation does not match")
    if len(password) < 12:
        raise SystemExit("administrator password must contain at least 12 characters")

    server_dir = args.server_dir.expanduser().resolve()
    docker = shutil.which("docker")
    if docker is None:
        raise SystemExit("Docker CLI was not found on PATH")
    command = [
        docker,
        "compose",
        "--env-file",
        str(server_dir / ".env"),
        "-f",
        str(server_dir / "compose.yml"),
        "exec",
        "-T",
        "external",
        "python",
        "-c",
        CONTAINER_SCRIPT,
    ]
    payload = json.dumps({"username": args.username, "password": password})
    result = subprocess.run(
        command,
        input=payload,
        text=True,
        capture_output=True,
        check=False,
    )
    password = ""
    payload = ""

    if result.returncode != 0:
        message = result.stderr.strip() or "administrator bootstrap failed"
        raise SystemExit(message)

    print(f"[OK] administrator created: {result.stdout.strip()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
