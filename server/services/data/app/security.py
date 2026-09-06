"""Authenticate internal requests and enforce per-service route scopes."""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import (
    Header,
    Request,
)

from .config import Settings
from .errors import ApiError

_ROUTE_SCOPES: dict[tuple[str, str], frozenset[str]] = {
    ("POST", "/internal/v1/object-jobs/analysis/requeue-unconfigured"): frozenset(
        {"analysis"}
    ),
    ("POST", "/internal/v1/object-jobs/identity/requeue-unconfigured"): frozenset(
        {"identity"}
    ),
    ("POST", "/internal/v1/object-jobs/identity/claim"): frozenset({"identity"}),
    ("POST", "/internal/v1/object-jobs/identity/{job_id}/complete"): frozenset(
        {"identity"}
    ),
    ("POST", "/internal/v1/object-jobs/analysis/claim"): frozenset({"analysis"}),
    ("POST", "/internal/v1/object-jobs/analysis/{job_id}/complete"): frozenset(
        {"analysis"}
    ),
    ("PUT", "/internal/v1/cameras/{camera_id}/objects"): frozenset({"inference"}),
    ("GET", "/internal/v1/cameras/enabled"): frozenset({"inference"}),
    ("PATCH", "/internal/v1/cameras/{camera_id}/status"): frozenset({"inference"}),
    ("POST", "/internal/v1/events"): frozenset({"external", "inference"}),
    ("POST", "/internal/v1/hooks/recording-complete"): frozenset({"media"}),
    ("POST", "/internal/v1/recording-segments"): frozenset({"recovery"}),
}


def require_internal_token(
    request: Request,
    x_internal_token: Annotated[str | None, Header()] = None,
) -> None:
    settings: Settings = request.app.state.settings
    configured_tokens = settings.data_api_tokens()
    route = request.scope.get("route")
    route_path = getattr(route, "path", request.url.path)
    allowed_scopes = _ROUTE_SCOPES.get(
        (request.method.upper(), route_path), frozenset({"external"})
    )
    expected_tokens = {
        configured_tokens[scope]
        for scope in allowed_scopes
        if configured_tokens.get(scope)
    }
    if not expected_tokens:
        raise ApiError(
            503,
            "INTERNAL_TOKEN_NOT_CONFIGURED",
            "Data Service 내부 인증 토큰이 설정되지 않았습니다.",
        )
    if x_internal_token is not None and any(
        hmac.compare_digest(x_internal_token, expected) for expected in expected_tokens
    ):
        return
    if x_internal_token is not None and any(
        hmac.compare_digest(x_internal_token, configured)
        for configured in set(configured_tokens.values())
        if configured
    ):
        raise ApiError(
            403,
            "INTERNAL_SCOPE_FORBIDDEN",
            "The service token is not authorized for this Data API operation.",
        )
    else:
        raise ApiError(401, "INVALID_INTERNAL_TOKEN", "내부 인증에 실패했습니다.")
