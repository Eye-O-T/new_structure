"""Request-scoped runtime settings and repository dependencies."""

from typing import Annotated, Any

from fastapi import Depends, Request

from .config import Settings
from .database.repositories import DataRepository


def get_repository(request: Request) -> DataRepository:
    return request.app.state.repository


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def _page(items: list[dict[str, Any]], limit: int, offset: int) -> dict[str, Any]:
    return {"items": items, "limit": limit, "offset": offset}


Repo = Annotated[DataRepository, Depends(get_repository)]
RuntimeSettings = Annotated[Settings, Depends(get_settings)]
