"""실제 배포 이미지·설치 패키지·모델 해시를 비밀값 없이 기록하는 읽기 전용 도구."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import subprocess
from datetime import datetime, timezone


SERVICES = {"data", "external", "preprocessing", "analysis", "mediamtx", "nginx"}


def execute(arguments):
    return subprocess.run(
        arguments,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=45,
    ).stdout


def json_rows(raw):
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return [json.loads(line) for line in raw.splitlines() if line.strip()]
    return value if isinstance(value, list) else [value]


def collect(server_dir: Path, env_file: Path, models: list[Path], runner=execute):
    if not env_file.is_file():
        raise ValueError("Deployment env file does not exist")
    rows = json_rows(
        runner(
            [
                "docker",
                "compose",
                "--env-file",
                str(env_file),
                "-f",
                str(server_dir / "compose.yml"),
                "ps",
                "--all",
                "--format",
                "json",
            ]
        )
    )
    services = {}
    for row in rows:
        name = row.get("Service")
        if name not in SERVICES:
            continue
        container_id = row["ID"]
        # 컨테이너 환경변수에는 비밀값이 있으므로 전체 inspect 대신 이미지 ID만 읽는다.
        image_id = runner(
            ["docker", "inspect", "--format", "{{.Image}}", container_id]
        ).strip()
        image = json_rows(runner(["docker", "image", "inspect", image_id]))[0]
        entry = {
            "container_id": container_id,
            "image_id": image["Id"],
            "repo_digests": image.get("RepoDigests") or [],
            "os": image.get("Os"),
            "architecture": image.get("Architecture"),
        }
        # 교체 예정인 Analysis 내부에는 명령을 실행하지 않는다.
        if (
            name in {"data", "external", "preprocessing"}
            and row.get("State") == "running"
        ):
            packages = json.loads(
                runner(
                    [
                        "docker",
                        "exec",
                        container_id,
                        "python",
                        "-m",
                        "pip",
                        "list",
                        "--format=json",
                        "--disable-pip-version-check",
                    ]
                )
            )
            entry["python_packages"] = sorted(
                (
                    {"name": item["name"], "version": item["version"]}
                    for item in packages
                ),
                key=lambda item: item["name"].lower(),
            )
        services[name] = entry
    if not services:
        raise ValueError("No deployed AI CCTV containers were found")
    artifacts = []
    for path in models:
        if path.suffix.lower() not in {".pt", ".onnx", ".engine"} or not path.is_file():
            raise ValueError("--model requires an existing .pt, .onnx or .engine file")
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        artifacts.append(
            {
                "filename": path.name,
                "bytes": path.stat().st_size,
                "sha256": digest.hexdigest(),
            }
        )
    return {
        "schema_version": 1,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "services": services,
        "missing_services": sorted(SERVICES - services.keys()),
        "models": artifacts,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--server-dir", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--model", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("Output already exists; select a new release record filename")
    try:
        result = collect(args.server_dir.resolve(), args.env_file.resolve(), args.model)
        # 실제 수집이 모두 끝난 뒤 새 파일만 만든다. 기존 인수 기록은 덮어쓰지 않는다.
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        parser.exit(
            1,
            f"Cannot record release: {type(error).__name__}; check Docker and file paths.\n",
        )
    print(f"Release manifest written: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
