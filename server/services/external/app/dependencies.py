# 요청에 필요한 설정·HTTP 클라이언트·로그인 제한·카메라 잠금을 공통 방식으로 제공한다.
"""Request-scoped settings, clients and camera lifecycle coordination."""

from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import AsyncIterator

from fastapi import Depends, Request

from .clients.data import DataClient
from .config import Settings
from .security.login_backoff import LoginBackoff


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings.from_env()


def get_settings_dependency() -> Settings:
    return get_settings()


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


async def hold_camera_lifecycle_lock(
    camera_id: str, request: Request
) -> AsyncIterator[None]:
    async with camera_lifecycle_lock(request, camera_id):
        yield
