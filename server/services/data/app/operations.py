"""Filesystem-aware operations kept outside the HTTP layer."""

from __future__ import annotations

import os
import re
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from ai_cctv_core.identifiers import safe_storage_path
from ai_cctv_core.time import format_utc, utc_now

from .config import Settings
from .errors import ApiError
from .repository import DataRepository
from .schemas import RecordingSegmentCreate, RetentionRequest


_CENTRAL_RECORDING_FILENAME = re.compile(
    r"^(?P<date>\d{8})T(?P<time>\d{6})-(?P<fraction>\d{1,9})Z\.mp4$"
)


def normalize_relative_path(root: Path, raw_path: str) -> tuple[str, Path]:
    # pathlib on Linux does not recognize a Windows drive or UNC path as absolute.
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
    """Accept a MediaMTX path only when it resolves below the recording root."""

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


def prepare_recording_hook(
    *,
    camera_id: str,
    segment_path: str,
    duration_seconds: float,
    settings: Settings,
) -> dict[str, Any]:
    if duration_seconds <= 0:
        raise ApiError(422, "INVALID_DURATION", "duration_seconds는 0보다 커야 합니다.")
    relative_path, target = normalize_hook_segment_path(
        settings.storage_root, segment_path
    )
    if not target.is_file():
        raise ApiError(
            422,
            "RECORDING_FILE_NOT_FOUND",
            "완료 Segment 파일을 저장소에서 찾을 수 없습니다.",
            {"relative_path": relative_path},
        )
    stat = target.stat()
    end_time = datetime.fromtimestamp(stat.st_mtime, UTC)
    start_time = end_time - timedelta(seconds=duration_seconds)
    suffix = target.suffix.lower()
    segment_format = "mpegts" if suffix in {".ts", ".mpegts"} else "fmp4"
    return {
        "camera_id": camera_id,
        "start_time": format_utc(start_time),
        "end_time": format_utc(end_time),
        "relative_path": relative_path,
        "format": segment_format,
        "codec": "h264",
        "duration_ms": round(duration_seconds * 1_000),
        "file_size": stat.st_size,
        "source": "central",
        "status": "ready",
        "checksum": None,
        "idempotency_key": f"recording-complete:{camera_id}:{relative_path}",
    }


def prepare_segment(
    payload: RecordingSegmentCreate, settings: Settings
) -> dict[str, Any]:
    relative_path, target = normalize_relative_path(
        settings.storage_root, payload.relative_path
    )
    duration_ms = int((payload.end_time - payload.start_time).total_seconds() * 1000)
    if payload.status.value == "ready":
        if not target.is_file():
            raise ApiError(
                422,
                "RECORDING_FILE_NOT_FOUND",
                "완료 Segment 파일을 저장소에서 찾을 수 없습니다.",
                {"relative_path": relative_path},
            )
        actual_size = target.stat().st_size
        if payload.file_size is not None and payload.file_size != actual_size:
            raise ApiError(
                409,
                "RECORDING_FILE_SIZE_MISMATCH",
                "요청 파일 크기와 실제 파일 크기가 다릅니다.",
                {"relative_path": relative_path},
            )
        file_size = actual_size
    else:
        file_size = payload.file_size or 0
    return {
        "camera_id": payload.camera_id,
        "start_time": format_utc(payload.start_time),
        "end_time": format_utc(payload.end_time),
        "relative_path": relative_path,
        "format": payload.format.value,
        "codec": payload.codec.lower(),
        "duration_ms": payload.duration_ms or duration_ms,
        "file_size": file_size,
        "source": payload.source.value,
        "status": payload.status.value,
        "checksum": payload.checksum,
        "idempotency_key": payload.idempotency_key,
    }


def _prepare_orphaned_central_segment(
    *,
    relative_path: str,
    target: Path,
    repository: DataRepository,
    settings: Settings,
) -> dict[str, Any] | None:
    """Rebuild metadata when a completed MediaMTX hook was not delivered."""

    parts = PurePosixPath(relative_path).parts
    if len(parts) != 5:
        return None
    camera_id, year, month, day, filename = parts
    match = _CENTRAL_RECORDING_FILENAME.fullmatch(filename)
    if match is None or repository.get_camera(camera_id) is None:
        return None

    fraction = match.group("fraction")[:6].ljust(6, "0")
    try:
        start_time = datetime.strptime(
            f"{match.group('date')}{match.group('time')}{fraction}",
            "%Y%m%d%H%M%S%f",
        ).replace(tzinfo=UTC)
    except ValueError:
        return None
    if (year, month, day) != (
        start_time.strftime("%Y"),
        start_time.strftime("%m"),
        start_time.strftime("%d"),
    ):
        return None

    stat = target.stat()
    modified_at = datetime.fromtimestamp(stat.st_mtime, UTC)
    if (utc_now() - modified_at).total_seconds() < settings.recovery_settle_seconds:
        # The active recorder may still be finalizing the newest path. A later
        # maintenance pass will index it after the settle window.
        return None
    expected_end = start_time + timedelta(
        seconds=settings.central_recording_segment_seconds
    )
    max_plausible_end = start_time + timedelta(
        seconds=settings.central_recording_segment_seconds * 2
    )
    end_time = (
        modified_at
        if start_time < modified_at <= max_plausible_end
        else expected_end
    )
    duration_ms = max(1, round((end_time - start_time).total_seconds() * 1_000))
    return {
        "camera_id": camera_id,
        "start_time": format_utc(start_time),
        "end_time": format_utc(end_time),
        "relative_path": relative_path,
        "format": "fmp4",
        "codec": "h264",
        "duration_ms": duration_ms,
        "file_size": stat.st_size,
        "source": "central",
        "status": "ready",
        "checksum": None,
        "idempotency_key": f"recording-reconcile:{camera_id}:{relative_path}",
    }


def reconcile(repository: DataRepository, settings: Settings) -> dict[str, Any]:
    missing: list[str] = []
    restored: list[str] = []
    corrupt: list[str] = []
    completed_deletions: list[str] = []
    deletion_retry_errors: list[str] = []
    known_paths: set[str] = set()
    for segment in repository.list_segments_for_reconcile():
        relative_path, target = normalize_relative_path(
            settings.storage_root, segment["relative_path"]
        )
        known_paths.add(relative_path)
        exists = target.is_file()
        if segment["status"] == "deleting":
            if exists:
                try:
                    target.unlink()
                except OSError:
                    # Keep the durable `deleting` marker so the next startup
                    # or maintenance pass retries the same idempotent unlink.
                    deletion_retry_errors.append(relative_path)
                    continue
            repository.set_segment_status(segment["id"], "deleted")
            completed_deletions.append(relative_path)
            continue
        if not exists and segment["status"] not in {"writing", "missing"}:
            repository.set_segment_status(segment["id"], "missing")
            missing.append(relative_path)
        elif exists and segment["status"] != "writing":
            if target.stat().st_size != segment["file_size"]:
                if segment["status"] != "corrupt":
                    repository.set_segment_status(segment["id"], "corrupt")
                corrupt.append(relative_path)
            elif segment["status"] in {"missing", "corrupt"}:
                repository.set_segment_status(segment["id"], "ready")
                restored.append(relative_path)

    orphaned: list[str] = []
    indexed_orphans: list[str] = []
    for path in settings.storage_root.rglob("*"):
        if not path.is_file():
            continue
        try:
            relative = (
                path.resolve().relative_to(settings.storage_root.resolve()).as_posix()
            )
        except ValueError:
            continue
        if relative in known_paths:
            continue
        existing = repository.get_segment_by_path(relative)
        if existing is not None:
            # A file that reappears after a durable `deleted` row is not a
            # missed hook. Keep it visible to operators instead of silently
            # resurrecting media removed by retention policy.
            orphaned.append(relative)
            continue
        values = _prepare_orphaned_central_segment(
            relative_path=relative,
            target=path,
            repository=repository,
            settings=settings,
        )
        if values is None:
            orphaned.append(relative)
            continue
        segment, created = repository.create_segment(values)
        if created:
            repository.link_segment_to_events(
                segment,
                settings.event_pre_roll_seconds,
                settings.event_post_roll_seconds,
            )
        known_paths.add(relative)
        indexed_orphans.append(relative)
    return {
        "missing": sorted(missing),
        "restored": sorted(restored),
        "corrupt": sorted(corrupt),
        "orphaned": sorted(orphaned),
        "indexed_orphans": sorted(indexed_orphans),
        "completed_deletions": sorted(completed_deletions),
        "deletion_retry_errors": sorted(deletion_retry_errors),
    }


def retention_cleanup(
    repository: DataRepository,
    settings: Settings,
    request: RetentionRequest,
) -> dict[str, Any]:
    cutoff_dt = request.before or (
        utc_now() - timedelta(days=request.retention_days or 0)
    )
    cutoff = format_utc(cutoff_dt)
    candidates = repository.retention_candidates(cutoff)
    if request.dry_run:
        return {
            "cutoff": cutoff,
            "dry_run": True,
            "segment_ids": [item["id"] for item in candidates],
            "deleted": 0,
        }

    deleted: list[int] = []
    for segment in candidates:
        _relative, target = normalize_relative_path(
            settings.storage_root, segment["relative_path"]
        )
        repository.set_segment_status(segment["id"], "deleting")
        try:
            target.unlink(missing_ok=True)
        except OSError as exc:
            repository.set_segment_status(segment["id"], segment["status"])
            raise ApiError(
                503,
                "RETENTION_DELETE_FAILED",
                "보관 기간이 지난 파일을 삭제하지 못했습니다.",
                {"segment_id": segment["id"]},
            ) from exc
        repository.set_segment_status(segment["id"], "deleted")
        deleted.append(segment["id"])
    return {
        "cutoff": cutoff,
        "dry_run": False,
        "segment_ids": deleted,
        "deleted": len(deleted),
    }


def storage_is_ready(settings: Settings) -> bool:
    roots = (settings.storage_root, settings.snapshot_root, settings.backup_root)
    return all(root.is_dir() and os.access(root, os.R_OK | os.W_OK) for root in roots)


def storage_usage(settings: Settings) -> dict[str, Any]:
    usage = shutil.disk_usage(settings.storage_root)
    free_percent = round((usage.free / usage.total) * 100, 2) if usage.total else 0.0
    return {
        "status": ("warning" if free_percent < settings.warning_free_percent else "ok"),
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "free_percent": free_percent,
        "warning_below_percent": settings.warning_free_percent,
    }
