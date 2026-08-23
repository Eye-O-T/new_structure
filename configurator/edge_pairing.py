"""Provision a discovered Edge after the central Camera registration succeeds."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .edge_discovery import DiscoveredEdge, pairing_completion_url


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


@dataclass(frozen=True)
class EdgePairingError(RuntimeError):
    message: str

    def __str__(self) -> str:
        return self.message


def complete_edge_pairing(
    edge: DiscoveredEdge,
    *,
    pairing_key: str,
    server_response: Mapping[str, Any],
    central_host: str,
    central_port: int,
    video_profile: str,
    backup_root: str = "/var/lib/ai-cctv-edge/recordings",
    timeout: float = 10.0,
) -> dict[str, Any]:
    if (
        len(pairing_key) < 32
        or pairing_key != pairing_key.strip()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in pairing_key)
    ):
        raise EdgePairingError("Edge pairing key is invalid")
    credentials = server_response.get("publish_credentials")
    camera_id = server_response.get("camera_id")
    if camera_id != edge.camera_id or not isinstance(credentials, Mapping):
        raise EdgePairingError("server registration identity does not match the Edge")
    username = credentials.get("username")
    password = credentials.get("password")
    if not isinstance(username, str) or not isinstance(password, str):
        raise EdgePairingError("server registration did not return publish credentials")
    normalized_host = central_host.strip()
    if (
        not normalized_host
        or normalized_host in {"0.0.0.0", "::"}
        or "://" in normalized_host
        or any(character.isspace() or character in "/@?#" for character in normalized_host)
    ):
        raise EdgePairingError("a reachable central RTSP host is required")
    if isinstance(central_port, bool) or not 1 <= central_port <= 65535:
        raise EdgePairingError("central RTSP port must be in range 1..65535")
    if video_profile not in edge.supported_profiles:
        raise EdgePairingError("selected video profile is not supported by the Edge")
    if not backup_root.startswith("/") or "\x00" in backup_root:
        raise EdgePairingError("Edge backup root must be an absolute POSIX path")
    body = json.dumps(
        {
            "device_id": edge.device_id,
            "camera_id": edge.camera_id,
            "central_host": normalized_host,
            "central_port": central_port,
            "backup_root": backup_root,
            "video_profile": video_profile,
            "supported_profiles": list(edge.supported_profiles),
            "publish_username": username,
            "publish_password": password,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = Request(
        pairing_completion_url(edge),
        data=body,
        method="PUT",
        headers={
            "Authorization": f"Bearer {pairing_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    opener = build_opener(ProxyHandler({}), _NoRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                raise EdgePairingError(f"Edge pairing returned HTTP {response.status}")
            raw = response.read(65_537)
    except HTTPError as exc:
        raise EdgePairingError(f"Edge pairing returned HTTP {exc.code}") from exc
    except (URLError, OSError, TimeoutError) as exc:
        raise EdgePairingError(f"Edge pairing connection failed: {exc}") from exc
    if len(raw) > 65_536:
        raise EdgePairingError("Edge pairing response is too large")
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise EdgePairingError("Edge pairing response is not UTF-8 JSON") from exc
    if not isinstance(result, dict) or result.get("status") != "configured":
        raise EdgePairingError("Edge did not confirm configuration")
    return result
