from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Mapping
from urllib.parse import urlsplit


CAMERA_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class PublishCredential:
    username: str
    password: str


def _read_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default

    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    raise RuntimeError(f"{name} must be a boolean value")


def _read_positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc

    if value <= 0:
        raise RuntimeError(f"{name} must be greater than zero")
    return value


def _read_ttl_seconds(
    seconds_name: str,
    legacy_name: str,
    legacy_multiplier: int,
    default_seconds: int,
) -> int:
    if os.getenv(seconds_name) is not None:
        return _read_positive_int(seconds_name, default_seconds)
    if os.getenv(legacy_name) is not None:
        return _read_positive_int(legacy_name, 1) * legacy_multiplier
    return default_seconds


def _read_publish_credentials() -> dict[str, PublishCredential]:
    raw = os.getenv("MEDIA_PUBLISH_CREDENTIALS_JSON", "{}")
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("MEDIA_PUBLISH_CREDENTIALS_JSON is invalid") from exc

    if not isinstance(decoded, dict):
        raise RuntimeError("MEDIA_PUBLISH_CREDENTIALS_JSON must be an object")

    credentials: dict[str, PublishCredential] = {}
    for camera_id, value in decoded.items():
        if not isinstance(camera_id, str) or not CAMERA_ID_PATTERN.fullmatch(camera_id):
            raise RuntimeError(
                "MEDIA_PUBLISH_CREDENTIALS_JSON has an invalid camera ID"
            )
        if not isinstance(value, dict):
            raise RuntimeError("MEDIA_PUBLISH_CREDENTIALS_JSON has an invalid entry")

        username = value.get("username")
        password = value.get("password")
        if not isinstance(username, str) or not username:
            raise RuntimeError("MEDIA_PUBLISH_CREDENTIALS_JSON has an invalid username")
        if not isinstance(password, str) or not password:
            raise RuntimeError("MEDIA_PUBLISH_CREDENTIALS_JSON has an invalid password")
        credentials[camera_id] = PublishCredential(username=username, password=password)

    return credentials


def _validate_http_url(name: str, value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise RuntimeError(f"{name} must be an HTTP(S) URL")
    if parsed.username or parsed.password:
        raise RuntimeError(f"{name} must not contain credentials")
    return value.rstrip("/")


@dataclass(frozen=True)
class Settings:
    data_base_url: str
    data_health_url: str
    internal_token: str
    jwt_secret: str
    jwt_issuer: str = "ai-cctv-external"
    jwt_audience: str = "ai-cctv"
    access_ttl_seconds: int = 900
    refresh_ttl_seconds: int = 604800
    access_cookie_name: str = "ai_cctv_access"
    refresh_cookie_name: str = "ai_cctv_refresh"
    cookie_secure: bool = True
    cookie_samesite: str = "lax"
    public_hls_prefix: str = "/hls"
    public_playback_prefix: str = "/playback"
    login_backoff_base_seconds: int = 1
    login_backoff_max_seconds: int = 60
    media_publish_credentials: Mapping[str, PublishCredential] = field(
        default_factory=dict
    )

    def __post_init__(self) -> None:
        _validate_http_url("DATA_BASE_URL", self.data_base_url)
        _validate_http_url("DATA_HEALTH_URL", self.data_health_url)
        if len(self.internal_token) < 16:
            raise RuntimeError(
                "INTERNAL_SERVICE_TOKEN must contain at least 16 characters"
            )
        if len(self.jwt_secret.encode("utf-8")) < 32:
            raise RuntimeError("JWT_SECRET must contain at least 32 bytes")
        if self.cookie_samesite not in {"lax", "strict", "none"}:
            raise RuntimeError("COOKIE_SAMESITE must be lax, strict, or none")
        if self.cookie_samesite == "none" and not self.cookie_secure:
            raise RuntimeError(
                "COOKIE_SECURE must be true when COOKIE_SAMESITE is none"
            )
        for prefix in (self.public_hls_prefix, self.public_playback_prefix):
            if not prefix.startswith("/") or ".." in prefix:
                raise RuntimeError("Public media prefixes must be absolute safe paths")

    @classmethod
    def from_env(cls) -> "Settings":
        data_base_url = os.getenv(
            "DATA_BASE_URL",
            os.getenv(
                "DATA_SERVICE_URL",
                "http://nginx:8080/internal/data/v1",
            ),
        )
        return cls(
            data_base_url=_validate_http_url("DATA_BASE_URL", data_base_url),
            data_health_url=_validate_http_url(
                "DATA_HEALTH_URL",
                os.getenv(
                    "DATA_HEALTH_URL",
                    "http://nginx:8080/internal/data/health/ready",
                ),
            ),
            internal_token=os.getenv("INTERNAL_SERVICE_TOKEN", ""),
            jwt_secret=os.getenv("JWT_SECRET", ""),
            jwt_issuer=os.getenv("JWT_ISSUER", "ai-cctv-external"),
            jwt_audience=os.getenv("JWT_AUDIENCE", "ai-cctv"),
            access_ttl_seconds=_read_ttl_seconds(
                "ACCESS_TOKEN_TTL_SECONDS",
                "ACCESS_TOKEN_MINUTES",
                60,
                900,
            ),
            refresh_ttl_seconds=_read_ttl_seconds(
                "REFRESH_TOKEN_TTL_SECONDS",
                "REFRESH_TOKEN_DAYS",
                86_400,
                604800,
            ),
            access_cookie_name=os.getenv("ACCESS_COOKIE_NAME", "ai_cctv_access"),
            refresh_cookie_name=os.getenv("REFRESH_COOKIE_NAME", "ai_cctv_refresh"),
            cookie_secure=_read_bool("COOKIE_SECURE", True),
            cookie_samesite=os.getenv("COOKIE_SAMESITE", "lax").lower(),
            public_hls_prefix=os.getenv("PUBLIC_HLS_PREFIX", "/hls").rstrip("/"),
            public_playback_prefix=os.getenv(
                "PUBLIC_PLAYBACK_PREFIX",
                "/playback",
            ).rstrip("/"),
            login_backoff_base_seconds=_read_positive_int(
                "LOGIN_BACKOFF_BASE_SECONDS",
                1,
            ),
            login_backoff_max_seconds=_read_positive_int(
                "LOGIN_BACKOFF_MAX_SECONDS",
                60,
            ),
            media_publish_credentials=_read_publish_credentials(),
        )
