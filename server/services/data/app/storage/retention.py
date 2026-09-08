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
        # 파일 삭제와 DB 갱신은 하나의 트랜잭션으로 묶을 수 없다.
        # 먼저 삭제 중 상태를 기록하여 중단되면 다음 파일 대조 작업이 이어서 처리하도록 한다.
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
