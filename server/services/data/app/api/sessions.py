# 로그인 갱신 토큰과 폐기된 접근 토큰을 저장하여 로그아웃과 토큰 교체를 지원한다.

from __future__ import annotations

from typing import Any

from fastapi import (
    APIRouter,
    Response,
    status,
)

from ai_cctv_core.time import format_utc

from ..dependencies import Repo
from ..errors import ApiError, _not_found
from ..schemas import (
    RefreshTokenCreate,
    RevokedTokenPut,
)

router = APIRouter()


# 교체할 토큰의 부재와 이미 소모된 토큰의 충돌을 구분해 갱신 실패를 전달한다.
@router.post("/tokens/refresh", status_code=status.HTTP_201_CREATED)
def issue_refresh_token(
    payload: RefreshTokenCreate, repository: Repo
) -> dict[str, Any]:
    values = payload.model_dump(mode="json")
    values["expires_at"] = format_utc(payload.expires_at)
    try:
        return repository.issue_refresh_token(values)
    except LookupError as exc:
        raise _not_found("refresh_token") from exc
    except PermissionError as exc:
        raise ApiError(409, "REFRESH_TOKEN_NOT_ROTATABLE", str(exc)) from exc


@router.get("/tokens/refresh/{jti}")
def get_refresh_token(jti: str, repository: Repo) -> dict[str, Any]:
    token = repository.get_refresh_token(jti)
    if token is None:
        raise _not_found("refresh_token")
    return token


@router.delete("/tokens/refresh/{jti}", status_code=status.HTTP_204_NO_CONTENT)
def delete_refresh_token(jti: str, repository: Repo) -> Response:
    if not repository.delete_refresh_token(jti):
        raise _not_found("refresh_token")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# 폐기 토큰 만료 시각을 DB 비교에 사용하는 UTC 형식으로 정규화한다.
@router.put("/tokens/revoked/{jti}")
def put_revoked_token(
    jti: str, payload: RevokedTokenPut, repository: Repo
) -> dict[str, Any]:
    values = payload.model_dump(mode="json")
    values["expires_at"] = format_utc(payload.expires_at)
    return repository.put_revoked_token(jti, values)


@router.get("/tokens/revoked/{jti}")
def get_revoked_token(jti: str, repository: Repo) -> dict[str, Any]:
    token = repository.get_revoked_token(jti)
    if token is None:
        raise _not_found("revoked_token")
    return token
