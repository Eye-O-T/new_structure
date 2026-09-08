# 실제 녹화 파일과 DB 목록을 대조하고 누락·손상·등록되지 않은 파일 상태를 정리한다.

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any

from ai_cctv_core.time import central_recording_start, format_utc, utc_now

from ..config import Settings
from ..database.repositories import DataRepository
from ..errors import ApiError
from ..schemas import RecordingSegmentCreate
from .paths import normalize_hook_segment_path, normalize_relative_path

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
    start_time = central_recording_start(target.name)
    if start_time is None:
        end_time = datetime.fromtimestamp(stat.st_mtime, UTC)
        start_time = end_time - timedelta(seconds=duration_seconds)
    else:
        # 파일 수정 시각은 저장 지연의 영향을 받으므로 녹화 시작은 파일명을 따른다.
        end_time = start_time + timedelta(seconds=duration_seconds)
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
    """MediaMTX 완료 통지를 놓친 녹화의 정보를 다시 등록한다."""

    parts = PurePosixPath(relative_path).parts
    if len(parts) != 5:
        return None
    camera_id, year, month, day, filename = parts
    start_time = central_recording_start(filename)
    if start_time is None or repository.get_camera(camera_id) is None:
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
        # 최신 파일은 기록 중일 수 있어 안정화 시간이 지난 다음 점검에서 등록한다.
        return None
    expected_end = start_time + timedelta(
        seconds=settings.central_recording_segment_seconds
    )
    max_plausible_end = start_time + timedelta(
        seconds=settings.central_recording_segment_seconds * 2
    )
    end_time = (
        modified_at if start_time < modified_at <= max_plausible_end else expected_end
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
                    # deleting 상태를 남겨 다음 시작·점검에서 같은 파일 삭제를 재시도한다.
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
            # 삭제 확정 뒤 다시 나타난 파일은 누락 통지로 보지 않는다. 자동 복원 대신 운영자에게 알린다.
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
