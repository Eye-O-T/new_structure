"""Authenticated management client shared by the Configurator GUI and CLI."""

from __future__ import annotations

import json
import os
import secrets
import socket
from collections.abc import Callable, Mapping
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .private_files import restrict_private_file


VIDEO_PROFILES = ("hd", "fhd")
# The central control endpoint can legitimately wait for Edge apply and rollback
# verification. Keep this above the server's 75-second Edge control deadline.
DEFAULT_REQUEST_TIMEOUT_SECONDS = 90.0
_SENSITIVE_PARTS = ("password", "token", "secret", "credential", "authorization")


class _NoRedirectHandler(HTTPRedirectHandler):
    """Keep credentials pinned to the administrator-selected origin."""

    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def _is_loopback(hostname: str) -> bool:
    if hostname.lower() == "localhost":
        return True
    try:
        return ip_address(hostname).is_loopback
    except ValueError:
        return False


def _validate_edge_url(value: str, name: str) -> None:
    parsed = urlsplit(value.strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{name} must be a credential-free HTTP(S) URL")
    if any(part == ".." for part in parsed.path.split("/")):
        raise ValueError(f"{name} contains an invalid path")


class ServerApiError(RuntimeError):
    """Safe, operator-facing representation of an External Service error."""

    def __init__(
        self,
        status_code: int | None,
        code: str,
        message: str,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details


def redact_for_display(value: Any) -> Any:
    """Recursively remove secrets before a response reaches UI or console output."""

    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower()
            if any(part in normalized for part in _SENSITIVE_PARTS):
                redacted[str(key)] = "[redacted]"
            else:
                redacted[str(key)] = redact_for_display(item)
        return redacted
    if isinstance(value, list):
        return [redact_for_display(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_for_display(item) for item in value)
    return value


def prepare_private_output(path: Path) -> Path:
    """Validate a private output location before requesting a one-time secret."""

    target = path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() and not target.is_file():
        raise ValueError("publish credentials output must be a file path")
    probe = target.with_name(f".{target.name}.{secrets.token_hex(8)}.probe")
    try:
        with probe.open("x", encoding="utf-8"):
            pass
        restrict_private_file(probe)
    finally:
        probe.unlink(missing_ok=True)
    return target


def write_publish_credentials(
    response: Mapping[str, Any], camera_id: str, path: Path
) -> Path:
    """Atomically persist the one-time RTSP publish credential, never a JWT."""

    credentials = response.get("publish_credentials")
    response_camera_id = response.get("camera_id")
    if response_camera_id != camera_id or not isinstance(credentials, Mapping):
        raise ServerApiError(
            None,
            "INVALID_SERVER_RESPONSE",
            "Server response did not contain publish credentials",
        )
    username = credentials.get("username")
    password = credentials.get("password")
    if not isinstance(username, str) or not username:
        raise ServerApiError(
            None, "INVALID_SERVER_RESPONSE", "Publish username is missing"
        )
    if not isinstance(password, str) or not password:
        raise ServerApiError(
            None, "INVALID_SERVER_RESPONSE", "Publish password is missing"
        )

    target = prepare_private_output(path)
    temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
    payload = {
        "camera_id": camera_id,
        "username": username,
        "password": password,
    }
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        restrict_private_file(temporary)
        os.replace(temporary, target)
        restrict_private_file(target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def _error_from_payload(status_code: int, payload: Any) -> ServerApiError:
    code = f"HTTP_{status_code}"
    message = f"Server request failed with HTTP {status_code}"
    details: Any = None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            code = str(error.get("code") or code)
            message = str(error.get("message") or message)
            details = error.get("details")
        else:
            reason_code = payload.get("reason_code")
            if reason_code:
                code = str(reason_code)
            detail = payload.get("detail", payload.get("message"))
            if isinstance(detail, str):
                message = detail
            elif isinstance(detail, list):
                messages = [
                    str(item.get("msg", item)) if isinstance(item, dict) else str(item)
                    for item in detail
                ]
                message = "; ".join(messages) or message
                details = detail
            elif detail is not None:
                details = detail
    return ServerApiError(status_code, code, message, details)


class ServerApiClient:
    """Small JSON client for the public, versioned External Service boundary."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout: float = DEFAULT_REQUEST_TIMEOUT_SECONDS,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        parsed = urlsplit(base_url.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("server URL must be an absolute HTTP(S) URL")
        if parsed.scheme != "https" and not _is_loopback(parsed.hostname):
            raise ValueError("server URL must use HTTPS except on the local loopback")
        if parsed.username or parsed.password:
            raise ValueError("server URL must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("server URL must not contain a query or fragment")
        if parsed.path not in {"", "/"}:
            raise ValueError("server URL must not contain a path")
        if timeout <= 0:
            raise ValueError("timeout must be greater than zero")
        self.base_url = base_url.strip().rstrip("/")
        self.timeout = timeout
        self._opener = (
            opener or build_opener(ProxyHandler({}), _NoRedirectHandler()).open
        )
        self._access_token: str | None = None
        self._refresh_token: str | None = None

    def login(self, username: str, password: str) -> dict[str, Any]:
        if not username.strip() or not password:
            raise ValueError("administrator username and password are required")
        response = self._request(
            "POST",
            "/api/v1/auth/login",
            {"username": username, "password": password},
            authenticated=False,
        )
        access_token = response.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise ServerApiError(
                None,
                "INVALID_AUTH_RESPONSE",
                "Login response did not contain an access token",
            )
        self._access_token = access_token
        refresh_token = response.get("refresh_token")
        self._refresh_token = (
            refresh_token if isinstance(refresh_token, str) and refresh_token else None
        )
        return redact_for_display(response)

    def register_edge(
        self,
        *,
        camera_id: str,
        name: str,
        edge_device_id: str,
        edge_management_url: str,
        edge_recovery_url: str,
        edge_auth_token: str,
    ) -> dict[str, Any]:
        if not all((camera_id.strip(), name.strip(), edge_device_id.strip())):
            raise ValueError("camera ID, name and Edge device ID are required")
        _validate_edge_url(edge_management_url, "Edge management URL")
        _validate_edge_url(edge_recovery_url, "Edge recovery URL")
        if len(edge_auth_token) < 32:
            raise ValueError("Edge auth token must contain at least 32 characters")
        return self._request(
            "POST",
            "/api/v1/cameras",
            {
                "camera_id": camera_id,
                "name": name,
                "edge_device_id": edge_device_id,
                "edge_management_url": edge_management_url,
                "edge_recovery_url": edge_recovery_url,
                "edge_auth_token": edge_auth_token,
                "enabled": True,
            },
        )

    def update_edge(
        self,
        camera_id: str,
        *,
        edge_device_id: str | None = None,
        edge_management_url: str | None = None,
        edge_recovery_url: str | None = None,
        edge_auth_token: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            key: value
            for key, value in {
                "edge_device_id": edge_device_id,
                "edge_management_url": edge_management_url,
                "edge_recovery_url": edge_recovery_url,
                "edge_auth_token": edge_auth_token,
            }.items()
            if value is not None
        }
        if not payload:
            raise ValueError("at least one Edge field must be supplied")
        if edge_device_id is not None and not edge_device_id.strip():
            raise ValueError("Edge device ID must not be empty")
        if edge_management_url is not None:
            _validate_edge_url(edge_management_url, "Edge management URL")
        if edge_recovery_url is not None:
            _validate_edge_url(edge_recovery_url, "Edge recovery URL")
        if edge_auth_token is not None and len(edge_auth_token) < 32:
            raise ValueError("Edge auth token must contain at least 32 characters")
        return self._request(
            "PATCH",
            f"/api/v1/cameras/{quote(camera_id, safe='')}",
            payload,
        )

    def rotate_publish_credentials(self, camera_id: str) -> dict[str, Any]:
        if not camera_id.strip():
            raise ValueError("camera ID is required")
        return self._request(
            "POST",
            (f"/api/v1/cameras/{quote(camera_id, safe='')}/publish-credentials/rotate"),
        )

    def camera_status(self, camera_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/v1/cameras/{quote(camera_id, safe='')}/status",
        )

    def video_profile(self, camera_id: str) -> dict[str, Any]:
        return self._request(
            "GET",
            f"/api/v1/cameras/{quote(camera_id, safe='')}/video-profile",
        )

    def set_video_profile(self, camera_id: str, profile: str) -> dict[str, Any]:
        if profile not in VIDEO_PROFILES:
            raise ValueError("video profile must be 'hd' or 'fhd'")
        return self._request(
            "PATCH",
            f"/api/v1/cameras/{quote(camera_id, safe='')}/video-profile",
            {"profile": profile},
        )

    def _request(
        self,
        method: str,
        path: str,
        payload: Mapping[str, Any] | None = None,
        *,
        authenticated: bool = True,
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        data = None
        if payload is not None:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
        if authenticated:
            if self._access_token is None:
                raise ServerApiError(
                    None, "AUTH_REQUIRED", "Log in before calling the management API"
                )
            headers["Authorization"] = f"Bearer {self._access_token}"
        request = Request(
            f"{self.base_url}{path}", data=data, headers=headers, method=method
        )
        try:
            with self._opener(request, timeout=self.timeout) as response:
                raw = response.read()
        except HTTPError as exc:
            raw = exc.read()
            try:
                body = json.loads(raw.decode("utf-8")) if raw else None
            except (UnicodeDecodeError, json.JSONDecodeError):
                body = None
            raise _error_from_payload(exc.code, body) from None
        except (URLError, TimeoutError, socket.timeout, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ServerApiError(
                None,
                "SERVER_UNREACHABLE",
                f"Cannot reach the server: {reason}",
            ) from None

        if not raw:
            return {}
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ServerApiError(
                None,
                "INVALID_SERVER_RESPONSE",
                "Server returned a non-JSON response",
            ) from None
        if not isinstance(decoded, dict):
            raise ServerApiError(
                None,
                "INVALID_SERVER_RESPONSE",
                "Server returned an unexpected JSON value",
            )
        return decoded
