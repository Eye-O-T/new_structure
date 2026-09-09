# 카메라 ID가 URL과 저장 경로에 함께 쓰이므로 허용 문자를 한곳에서 제한한다.

from __future__ import annotations

import re
from pathlib import Path

CAMERA_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


# URL·파일명에 그대로 사용할 수 있는 소문자 식별자만 반환한다.
def validate_camera_id(value: str) -> str:
    if not CAMERA_ID_PATTERN.fullmatch(value):
        raise ValueError("camera_id must match ^[a-z0-9][a-z0-9_-]{0,63}$")
    return value


def validate_stream_path(value: str) -> str:
    """MediaMTX 경로가 유효한 단일 카메라 ID인지 검사한다."""

    return validate_camera_id(value)


def safe_storage_path(root: str | Path, relative_path: str | Path) -> Path:
    """DB의 상대 경로를 저장소 내부 경로로 변환하고 절대 경로·탈출을 거부한다."""

    relative = Path(relative_path)
    if relative.is_absolute():
        raise ValueError("storage path must be relative")

    base = Path(root).resolve()
    # '..'이나 심볼릭 링크를 실제 경로로 해석한 뒤 저장소 바깥으로 벗어나는지 검사한다.
    candidate = (base / relative).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ValueError("storage path escapes the configured root") from exc
    return candidate
