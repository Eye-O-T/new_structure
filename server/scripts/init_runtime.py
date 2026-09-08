# Compose가 마운트할 호스트 저장 폴더를 미리 만든다.
# config.yaml·비밀값·모델은 생성하지 않으므로 별도로 준비해야 한다.


from __future__ import annotations

import argparse
from pathlib import Path


DIRECTORIES = (
    "database",
    "recordings",
    "recovered",
    "snapshots",
    "models",
    "logs",
    "certificates",
)


def parse_args() -> argparse.Namespace:
    server_dir = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--runtime-root",
        type=Path,
        default=server_dir / "runtime",
        help="Host runtime root (default: server/runtime)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.runtime_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)

    for relative_name in DIRECTORIES:
        path = root / relative_name
        path.mkdir(parents=True, exist_ok=True)
        print(f"[OK] {path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
