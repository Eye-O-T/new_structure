# 최신 객체 좌표와 인물 식별·분석 작업의 가져오기, 완료 보고를 제공한다.

from __future__ import annotations

from fastapi import (
    APIRouter,
)

from ai_cctv_core.contracts.objects import LiveObjects, ObjectJobCompletion

from ..dependencies import Repo
from ..errors import ApiError, _not_found

router = APIRouter()


# 존재하는 카메라에 한해 관측을 전달하며 최신 시각 여부는 저장소가 판단한다.
@router.put("/cameras/{camera_id}/objects")
def put_objects(camera_id: str, payload: LiveObjects, repository: Repo):
    if repository.get_camera(camera_id) is None:
        raise _not_found("camera")
    repository.put_live_objects(camera_id, payload)
    return {"accepted": True}


# 없는 카메라의 404와 관측이 오래되어 빈 객체 목록이 된 상태를 구분한다.
@router.get("/cameras/{camera_id}/objects")
def get_objects(camera_id: str, repository: Repo):
    if repository.get_camera(camera_id) is None:
        raise _not_found("camera")
    return repository.get_live_objects(camera_id)


# 식별 충돌 등 결과 적용 불가를 409로 변환하는 두 처리 단계의 공통 완료 경계이다.
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
