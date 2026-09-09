# 새 소스 배포에 필요한 역할별 내부 토큰과 카메라 게시 계정을 생성한다.
# 운영 중인 설치의 토큰 보존 이전에는 enable_object_processing 도구를 사용한다.


from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from server.setup.private_files import restrict_private_file  # noqa: E402


CAMERA_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


# 초기 배포용 출력 폴더·카메라 목록·명시적 덮어쓰기 옵션을 읽는다.
def parse_args() -> argparse.Namespace:
    server_dir = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=server_dir / "secrets",
        help="directory for data.env, external.env, preprocessing.env, media.env, and analysis.env",
    )
    parser.add_argument(
        "--camera-id",
        action="append",
        default=[],
        help=(
            "bootstrap-only Camera publish credential to generate; may be repeated "
            "and does not rotate a DB-backed credential"
        ),
    )
    parser.add_argument("--force", action="store_true")
    return parser.parse_args()


# 생성 토큰을 dotenv 단일 인용값으로 만들되 지원하지 않는 따옴표·개행은 거부한다.
def single_quote(value: str) -> str:
    if "'" in value or "\n" in value or "\r" in value:
        raise ValueError("dotenv value contains an unsupported character")
    return f"'{value}'"


# 완성된 임시 파일에 접근 권한을 적용한 다음 교체하여 일부만 저장된 비밀 파일을 피한다.
def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent, text=True
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        restrict_private_file(temporary_path)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


# 카메라 ID를 검증·중복 제거하고 역할별 독립 토큰을 생성한다. 기존 파일은 --force 없이는 거부한다.
def main() -> int:
    args = parse_args()

    camera_ids = list(dict.fromkeys(args.camera_id))
    for camera_id in camera_ids:
        if CAMERA_ID_PATTERN.fullmatch(camera_id) is None:
            raise SystemExit(f"invalid camera ID: {camera_id}")

    publish_credentials = {
        camera_id: {
            "username": camera_id,
            "password": secrets.token_urlsafe(32),
        }
        for camera_id in camera_ids
    }

    data_external_token = secrets.token_urlsafe(48)
    data_inference_token = secrets.token_urlsafe(48)
    data_media_token = secrets.token_urlsafe(48)
    data_recovery_token = secrets.token_urlsafe(48)
    data_identity_token = secrets.token_urlsafe(48)
    data_analysis_token = secrets.token_urlsafe(48)
    jwt_secret = secrets.token_urlsafe(48)
    media_read_username = "inference-reader"
    media_read_password = secrets.token_urlsafe(48)
    media_credentials = single_quote(
        json.dumps(publish_credentials, separators=(",", ":"), sort_keys=True)
    )
    output_dir = args.output_dir.expanduser().resolve()
    outputs = {
        output_dir / "data.env": [
            "# Data 전용 비밀값: Git에 포함하지 않는다.",
            f"DATA_EXTERNAL_TOKEN={data_external_token}",
            f"DATA_INFERENCE_TOKEN={data_inference_token}",
            f"DATA_MEDIA_TOKEN={data_media_token}",
            f"DATA_RECOVERY_TOKEN={data_recovery_token}",
            f"DATA_IDENTITY_TOKEN={data_identity_token}",
            f"DATA_ANALYSIS_TOKEN={data_analysis_token}",
            "",
        ],
        output_dir / "external.env": [
            "# External 전용 비밀값: Git에 포함하지 않는다.",
            f"DATA_EXTERNAL_TOKEN={data_external_token}",
            f"JWT_SECRET={jwt_secret}",
            f"MEDIA_READ_USERNAME={media_read_username}",
            f"MEDIA_READ_PASSWORD={media_read_password}",
            "# 최초 등록 전용이며, 운영 중 변경은 서버의 인증된 재발급 API를 사용한다.",
            f"MEDIA_PUBLISH_CREDENTIALS_JSON={media_credentials}",
            "",
        ],
        output_dir / "preprocessing.env": [
            "# Preprocessing 전용 비밀값: Git에 포함하지 않는다.",
            f"DATA_IDENTITY_TOKEN={data_identity_token}",
            f"DATA_INFERENCE_TOKEN={data_inference_token}",
            f"MEDIA_READ_USERNAME={media_read_username}",
            f"MEDIA_READ_PASSWORD={media_read_password}",
            "",
        ],
        output_dir / "analysis.env": [
            f"DATA_ANALYSIS_TOKEN={data_analysis_token}",
            "",
        ],
        output_dir / "media.env": [
            "# MediaMTX 녹화 완료 알림 전용: Git에 포함하지 않는다.",
            f"DATA_MEDIA_TOKEN={data_media_token}",
            "",
        ],
    }
    existing = [path for path in outputs if path.exists()]
    if existing and not args.force:
        paths = ", ".join(str(path) for path in existing)
        raise SystemExit(f"refusing to overwrite existing file(s): {paths}")
    for output, lines in outputs.items():
        atomic_write(output, "\n".join(lines))

    print("[OK] generated " + ", ".join(str(path) for path in outputs))
    if camera_ids:
        print(
            "[INFO] generated bootstrap-only publish credentials for: "
            + ", ".join(camera_ids)
            + "; this does not rotate DB-backed credentials"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
