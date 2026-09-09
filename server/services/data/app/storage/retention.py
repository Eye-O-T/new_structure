# 보관 기한이 지난 녹화를 정리하고 저장 공간의 용량과 쓰기 가능 여부를 확인한다.

from __future__ import annotations

import os
import shutil
from datetime import timedelta
from typing import Any

from ai_cctv_core.time import format_utc, utc_now

from ..config import Settings
from ..database.repositories import DataRepository
from ..errors import ApiError
from ..schemas import RetentionRequest
from .paths import normalize_relative_path
from .protection import SnapshotProtectionReader
from .snapshot_scan import SnapshotScanner

MAX_RETENTION_BATCHES = 10
MAX_SNAPSHOT_DELETIONS = 1000


# dry_run에서는 후보만 보고하고 실제 실행에서는 삭제 진행 상태를 기록하며 순서대로 지운다.
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
    protection_reader = SnapshotProtectionReader(settings)
    protection = protection_reader.read()
    if request.dry_run:
        return {
            "cutoff": cutoff,
            "dry_run": True,
            "segment_ids": [item["id"] for item in candidates],
            "deleted": 0,
            "snapshot_protection": protection.status(),
        }

    deleted: list[int] = []
    errors: list[dict[str, object]] = []
    for _ in range(MAX_RETENTION_BATCHES):
        for segment in candidates:
            _relative, target = normalize_relative_path(
                settings.storage_root, segment["relative_path"]
            )
            # 파일 삭제와 DB 갱신은 하나의 트랜잭션으로 묶을 수 없다.
            # deleting을 남겨 중단·파일 잠금 뒤 다음 대조 작업이 이어서 처리하도록 한다.
            repository.set_segment_status(segment["id"], "deleting")
            try:
                target.unlink(missing_ok=True)
            except OSError:
                errors.append(
                    {"code": "RETENTION_DELETE_FAILED", "segment_id": segment["id"]}
                )
                continue
            repository.set_segment_status(segment["id"], "deleted")
            deleted.append(segment["id"])
        if len(candidates) < 1000:
            break
        candidates = repository.retention_candidates(cutoff)

    now = utc_now()
    stamp = format_utc(now)
    job_cutoff = format_utc(now - timedelta(days=settings.job_max_age_days))
    expired_jobs = 0
    for _ in range(MAX_RETENTION_BATCHES):
        count = repository.expire_abandoned_object_jobs(job_cutoff, stamp)
        expired_jobs += count
        if count < 1000:
            break
    history: dict[str, int] = {}
    for _ in range(MAX_RETENTION_BATCHES):
        # 여러 배치가 진행되는 동안 outbox가 추가·갱신됐을 수 있다.
        protection = protection_reader.read()
        counts = repository.purge_expired_history(
            cutoff,
            stamp,
            protected_events=protection.events,
            protected_paths=protection.paths,
            purge_events=protection.ready,
        )
        for table, count in counts.items():
            history[table] = history.get(table, 0) + count
        if all(count < 1000 for count in counts.values()):
            break

    snapshot_deleted = 0
    scan = {"examined": 0, "cycle_complete": False}
    if protection.ready:
        # 새 파일 생성과 outbox 등록 사이도 안전해야 하므로 최소 하루는 파일을 보존한다.
        snapshot_cutoff = min(cutoff_dt, now - timedelta(days=1)).timestamp()
        scanner = getattr(repository, "snapshot_scanner", None)
        if scanner is None:
            scanner = SnapshotScanner(settings.snapshot_root, repository.database)
            repository.snapshot_scanner = scanner
        paths, scan = scanner.scan(candidate_limit=MAX_SNAPSHOT_DELETIONS)
        for path in paths:
            try:
                if path.stat().st_mtime >= snapshot_cutoff:
                    continue
                relative = path.relative_to(settings.snapshot_root.resolve()).as_posix()
                _relative, target = normalize_relative_path(
                    settings.snapshot_root, relative
                )
                # 보호 목록은 원자 교체되며 오래된 목록을 사용한 삭제를 피하도록 직전에 다시 읽는다.
                current = protection_reader.read()
                if not current.ready:
                    protection = current
                    break
                if relative in current.paths:
                    continue
                # 참조 확인과 삭제 사이에 Data 이벤트가 새로 등록되지 못하게 짧게 잠근다.
                with repository.database.transaction() as connection:
                    referenced = connection.execute(
                        "SELECT 1 FROM events WHERE snapshot_path=? "
                        "OR json_extract(metadata_json,'$.object.crop_path')=? "
                        "OR json_extract(metadata_json,'$.object.annotated_snapshot_path')=? LIMIT 1",
                        (relative, relative, relative),
                    ).fetchone()
                    if referenced is not None:
                        continue
                    target.unlink(missing_ok=True)
                snapshot_deleted += 1
                if snapshot_deleted >= MAX_SNAPSHOT_DELETIONS:
                    break
            except FileNotFoundError:
                continue
            except (OSError, ApiError):
                errors.append({"code": "SNAPSHOT_DELETE_FAILED"})
    return {
        "cutoff": cutoff,
        "dry_run": False,
        "segment_ids": deleted,
        "deleted": len(deleted),
        "snapshots_deleted": snapshot_deleted,
        "snapshot_scan": scan,
        "snapshot_protection": protection.status(),
        "expired_object_jobs": expired_jobs,
        "history_deleted": history,
        "errors": errors,
    }


# 녹화·스냅샷·백업 폴더가 모두 존재하고 읽기·쓰기가 가능한지 확인한다.
def storage_is_ready(settings: Settings) -> bool:
    roots = (
        settings.storage_root,
        settings.snapshot_root,
        settings.database_path.parent,
        settings.backup_root,
    )
    return all(root.is_dir() and os.access(root, os.R_OK | os.W_OK) for root in roots)


# 녹화 저장소가 속한 디스크의 여유 비율을 계산하여 경고 임계값과 비교한다.
def storage_usage(settings: Settings) -> dict[str, Any]:
    volumes = {}
    for name, root in {
        "recordings": settings.storage_root,
        "snapshots": settings.snapshot_root,
        "database": settings.database_path.parent,
        "backups": settings.backup_root,
    }.items():
        usage = shutil.disk_usage(root)
        free_percent = (
            round((usage.free / usage.total) * 100, 2) if usage.total else 0.0
        )
        volumes[name] = {
            "status": "warning"
            if free_percent < settings.warning_free_percent
            else "ok",
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
            "free_percent": free_percent,
            "warning_below_percent": settings.warning_free_percent,
        }
    return {
        **volumes["recordings"],
        "status": "warning"
        if any(v["status"] == "warning" for v in volumes.values())
        else "ok",
        "volumes": volumes,
    }
