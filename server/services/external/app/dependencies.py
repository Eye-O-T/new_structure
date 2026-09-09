# 요청에 필요한 설정·HTTP 클라이언트·로그인 제한·카메라 잠금을 공통 방식으로 제공한다.

from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import AsyncIterator

from fastapi import Depends, Request

from .clients.data import DataClient
from .config import Settings
from .security.login_backoff import LoginBackoff


# 프로세스에서 사용하는 환경 설정을 한 번 읽어 의존성 호출 간 같은 인스턴스를 공유한다.
@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()


def get_settings_dependency() -> Settings:
    return get_settings()


# 주입한 Data 대역을 우선 사용하고 없는 경우 앱 수명 동안 재사용할 HTTP 클라이언트를 만든다.
async def get_data_client(
    request: Request,
    settings: Settings = Depends(get_settings_dependency),
) -> DataClient:
    existing = getattr(request.app.state, "data_client", None)
    if existing is not None:
        return existing

    client = DataClient(
        base_url=settings.data_base_url,
        health_url=settings.data_health_url,
        internal_token=settings.internal_token,
    )
    request.app.state.data_client = client
    return client


# 실패 이력이 요청마다 초기화되지 않도록 앱 상태에 로그인 제한 객체를 보관한다.
def get_login_backoff(
    request: Request,
    settings: Settings = Depends(get_settings_dependency),
) -> LoginBackoff:
    existing = getattr(request.app.state, "login_backoff", None)
    if existing is not None:
        return existing

    backoff = LoginBackoff(
        settings.login_backoff_base_seconds,
        settings.login_backoff_max_seconds,
    )
    request.app.state.login_backoff = backoff
    return backoff


def camera_lifecycle_lock(request: Request, camera_id: str) -> asyncio.Lock:
    return request.app.state.camera_lifecycle_lock_factory(camera_id)


# 요청 처리 동안 카메라 잠금을 유지하고 의존성 종료 시 자동으로 해제한다.
async def hold_camera_lifecycle_lock(
    camera_id: str, request: Request
) -> AsyncIterator[None]:
    async with camera_lifecycle_lock(request, camera_id):
        yield
