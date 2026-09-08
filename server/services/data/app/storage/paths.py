# 받은 파일 경로가 지정된 저장소 안에 있는지 확인해 다른 파일 접근을 막는다.

from __future__ import annotations

from pathlib import Path, PureWindowsPath

from ai_cctv_core.identifiers import safe_storage_path

from ..errors import ApiError


def normalize_relative_path(root: Path, raw_path: str) -> tuple[str, Path]:
    # Linux는 Windows 드라이브·공유 경로를 절대 경로로 보지 않으므로 별도로 검사한다.
    windows_path = PureWindowsPath(raw_path)
    if windows_path.is_absolute() or windows_path.drive:
        raise ApiError(422, "INVALID_STORAGE_PATH", "저장 경로는 상대 경로여야 합니다.")
    normalized = raw_path.replace("\\", "/")
    try:
        resolved = safe_storage_path(root, normalized)
    except ValueError as exc:
        raise ApiError(422, "INVALID_STORAGE_PATH", str(exc)) from exc
    relative = resolved.relative_to(root.resolve()).as_posix()
    if relative in {"", "."}:
        raise ApiError(422, "INVALID_STORAGE_PATH", "파일 경로가 필요합니다.")
    return relative, resolved


def normalize_hook_segment_path(root: Path, raw_path: str) -> tuple[str, Path]:
    """녹화 저장소 안으로 해석되는 MediaMTX 경로만 허용한다."""

    candidate = Path(raw_path)
    if not candidate.is_absolute():
        return normalize_relative_path(root, raw_path)
    resolved = candidate.resolve()
    try:
        relative = resolved.relative_to(root.resolve()).as_posix()
    except ValueError as exc:
        raise ApiError(
            422,
            "INVALID_STORAGE_PATH",
            "MediaMTX Segment가 녹화 저장소 밖을 가리킵니다.",
        ) from exc
    return relative, resolved
