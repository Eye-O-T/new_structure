# 복구 작업 조회, 녹화 파일 대조, 보관 기간 정리와 DB 백업을 요청하는 API이다.

from __future__ import annotations

from typing import Annotated, Any

from fastapi import (
    APIRouter,
    Query,
    status,
)

from ai_cctv_core.time import format_utc, utc_now

from ..dependencies import Repo, RuntimeSettings, _page
from ..errors import ApiError, _not_found
from ..schemas import (
    BackupRequest,
    RetentionRequest,
)
from ..storage.paths import normalize_relative_path
from ..storage.recordings import reconcile
from ..storage.retention import retention_cleanup

router = APIRouter()


@router.get("/recovery-jobs")
def list_recovery_jobs(
    repository: Repo,
    camera_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    return _page(repository.list_recovery_jobs(camera_id, limit, offset), limit, offset)


@router.get("/recovery-jobs/{job_id}")
def get_recovery_job(job_id: int, repository: Repo) -> dict[str, Any]:
    job = repository.get_recovery_job(job_id)
    if job is None:
        raise _not_found("recovery_job")
    return job


@router.post("/reconcile")
def reconcile_storage(repository: Repo, settings: RuntimeSettings) -> dict[str, Any]:
    return reconcile(repository, settings)


# 후보 확인과 실제 삭제의 구분은 요청의 dry_run 값을 그대로 따른다.
@router.post("/retention/cleanup")
def clean_retention(
    payload: RetentionRequest,
    repository: Repo,
    settings: RuntimeSettings,
) -> dict[str, Any]:
    return retention_cleanup(repository, settings, payload)


# 백업 루트 안의 새 경로만 허용하며 기존 백업 파일은 덮어쓰지 않는다.
@router.post("/backup", status_code=status.HTTP_201_CREATED)
def backup_database(
    payload: BackupRequest,
    repository: Repo,
    settings: RuntimeSettings,
) -> dict[str, Any]:
    filename = payload.filename or (
        "ai_cctv_" + format_utc(utc_now()).replace(":", "").replace("-", "") + ".db"
    )
    _relative, destination = normalize_relative_path(settings.backup_root, filename)
    if destination.exists():
        raise ApiError(409, "BACKUP_EXISTS", "동일한 이름의 백업이 이미 있습니다.")
    repository.database.backup(destination)
    relative = destination.relative_to(settings.backup_root.resolve()).as_posix()
    return {"relative_path": relative, "file_size": destination.stat().st_size}
