# 최신 객체 좌표와 인물 식별·분석 작업의 가져오기, 완료 보고를 제공한다.

from __future__ import annotations

from fastapi import (
    APIRouter,
)

from ai_cctv_core.contracts.objects import LiveObjects, ObjectJobCompletion

from ..dependencies import Repo
from ..errors import ApiError, _not_found

router = APIRouter()


@router.put("/cameras/{camera_id}/objects")
def put_objects(camera_id: str, payload: LiveObjects, repository: Repo):
    if repository.get_camera(camera_id) is None:
        raise _not_found("camera")
    repository.put_live_objects(camera_id, payload)
    return {"accepted": True}


@router.get("/cameras/{camera_id}/objects")
def get_objects(camera_id: str, repository: Repo):
    if repository.get_camera(camera_id) is None:
        raise _not_found("camera")
    return repository.get_live_objects(camera_id)


def complete_object(stage, job_id, payload, repository):
    try:
        return {"accepted": repository.complete_object_job(stage, job_id, payload)}
    except ValueError as exc:
        raise ApiError(409, "OBJECT_RESULT_CONFLICT", str(exc)) from exc


@router.post("/object-jobs/identity/requeue-unconfigured")
def requeue_identity(repository: Repo):
    return {"requeued": repository.requeue_unconfigured_objects("identity")}


@router.post("/object-jobs/analysis/requeue-unconfigured")
def requeue_analysis(repository: Repo):
    return {"requeued": repository.requeue_unconfigured_objects("analysis")}


@router.post("/object-jobs/identity/claim")
def claim_identity(repository: Repo):
    return {"job": repository.claim_object_job("identity")}


@router.post("/object-jobs/analysis/claim")
def claim_analysis(repository: Repo):
    return {"job": repository.claim_object_job("analysis")}


@router.post("/object-jobs/identity/{job_id}/complete")
def complete_identity(job_id: int, payload: ObjectJobCompletion, repository: Repo):
    return complete_object("identity", job_id, payload, repository)


@router.post("/object-jobs/analysis/{job_id}/complete")
def complete_analysis(job_id: int, payload: ObjectJobCompletion, repository: Repo):
    return complete_object("analysis", job_id, payload, repository)
