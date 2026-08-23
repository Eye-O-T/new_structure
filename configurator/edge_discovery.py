"""Discover HMAC-authenticated AI_CCTV Edge pairing advertisements."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import socket
import time
import uuid
from dataclasses import dataclass
from urllib.parse import urlsplit

DISCOVERY_PORT = 37020
DISCOVERY_MESSAGE_TYPE = "AI_CCTV_EDGE_ADVERTISE"
DISCOVERY_VERSION = 1
MAX_DISCOVERY_PACKET = 8192
_EXACT_FIELDS = {
    "message_type",
    "version",
    "message_id",
    "sent_at",
    "device_id",
    "camera_id",
    "management_port",
    "recovery_port",
    "supported_profiles",
    "signature",
}
_CAMERA_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class DiscoveredEdge:
    device_id: str
    camera_id: str
    address: str
    management_url: str
    recovery_url: str
    supported_profiles: tuple[str, ...]
    message_id: str
    sent_at: int


def _canonical_payload(message: dict[str, object]) -> bytes:
    return json.dumps(
        message,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def parse_advertisement(
    data: bytes,
    peer_address: str,
    pairing_key: str,
    *,
    now: int | None = None,
    max_age_seconds: int = 10,
) -> DiscoveredEdge:
    if len(pairing_key) < 32:
        raise ValueError("Edge pairing key must contain at least 32 characters")
    if not isinstance(data, bytes) or len(data) > MAX_DISCOVERY_PACKET:
        raise ValueError("invalid discovery packet size")
    try:
        message = json.loads(data.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("discovery packet is not UTF-8 JSON") from exc
    if not isinstance(message, dict) or set(message) != _EXACT_FIELDS:
        raise ValueError("discovery packet fields do not match the protocol")
    if message["message_type"] != DISCOVERY_MESSAGE_TYPE or (
        isinstance(message["version"], bool)
        or message["version"] != DISCOVERY_VERSION
    ):
        raise ValueError("unsupported discovery protocol")
    identifier = message["message_id"]
    if not isinstance(identifier, str):
        raise ValueError("invalid discovery message_id")
    try:
        parsed_id = uuid.UUID(identifier)
    except ValueError as exc:
        raise ValueError("invalid discovery message_id") from exc
    if parsed_id.version != 4 or str(parsed_id) != identifier:
        raise ValueError("invalid discovery message_id")
    sent_at = message["sent_at"]
    if isinstance(sent_at, bool) or not isinstance(sent_at, int):
        raise ValueError("invalid discovery timestamp")
    current = int(time.time()) if now is None else now
    if abs(current - sent_at) > max_age_seconds:
        raise ValueError("stale discovery packet")
    device_id = message["device_id"]
    camera_id = message["camera_id"]
    if (
        not isinstance(device_id, str)
        or not 1 <= len(device_id) <= 128
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in device_id)
    ):
        raise ValueError("invalid Edge device_id")
    if not isinstance(camera_id, str) or not _CAMERA_ID.fullmatch(camera_id):
        raise ValueError("invalid camera_id")
    management_port = _port(message["management_port"])
    recovery_port = _port(message["recovery_port"])
    if management_port == recovery_port:
        raise ValueError("Edge ports must differ")
    raw_profiles = message["supported_profiles"]
    if not isinstance(raw_profiles, list) or not raw_profiles:
        raise ValueError("invalid supported profiles")
    profiles = tuple(raw_profiles)
    if any(not isinstance(item, str) for item in profiles):
        raise ValueError("invalid supported profiles")
    if len(set(profiles)) != len(profiles) or any(
        item not in {"hd", "fhd"} for item in profiles
    ):
        raise ValueError("invalid supported profiles")
    try:
        socket.inet_aton(peer_address)
    except OSError as exc:
        raise ValueError("discovery peer must be IPv4") from exc
    signature = message["signature"]
    if not isinstance(signature, str) or len(signature) != 64:
        raise ValueError("invalid discovery signature")
    try:
        int(signature, 16)
    except ValueError as exc:
        raise ValueError("invalid discovery signature") from exc
    unsigned = dict(message)
    del unsigned["signature"]
    expected = hmac.new(
        pairing_key.encode("utf-8"), _canonical_payload(unsigned), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("discovery signature does not match the pairing key")
    return DiscoveredEdge(
        device_id=device_id,
        camera_id=camera_id,
        address=peer_address,
        management_url=f"http://{peer_address}:{management_port}",
        recovery_url=f"http://{peer_address}:{recovery_port}",
        supported_profiles=profiles,
        message_id=identifier,
        sent_at=sent_at,
    )


def _port(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 65535:
        raise ValueError("invalid Edge port")
    return value


def discover_edges(
    pairing_key: str,
    *,
    timeout: float = 3.0,
    port: int = DISCOVERY_PORT,
    bind_host: str = "0.0.0.0",
    max_results: int = 16,
) -> list[DiscoveredEdge]:
    if len(pairing_key) < 32:
        raise ValueError("Edge pairing key must contain at least 32 characters")
    if timeout <= 0 or not 1 <= port <= 65535 or max_results <= 0:
        raise ValueError("invalid discovery settings")
    deadline = time.monotonic() + timeout
    results: dict[str, DiscoveredEdge] = {}
    seen_message_ids: set[str] = set()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp_socket:
        udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        udp_socket.bind((bind_host, port))
        while len(results) < max_results:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            udp_socket.settimeout(remaining)
            try:
                data, peer = udp_socket.recvfrom(MAX_DISCOVERY_PACKET + 1)
            except (socket.timeout, TimeoutError):
                break
            except OSError:
                break
            try:
                item = parse_advertisement(data, peer[0], pairing_key)
            except ValueError:
                continue
            if item.message_id in seen_message_ids:
                continue
            seen_message_ids.add(item.message_id)
            existing = results.get(item.device_id)
            if existing is None or item.sent_at > existing.sent_at:
                results[item.device_id] = item
    return sorted(results.values(), key=lambda item: (item.device_id, item.address))


def pairing_completion_url(edge: DiscoveredEdge) -> str:
    parsed = urlsplit(edge.management_url)
    if parsed.scheme != "http" or not parsed.hostname or parsed.path not in {"", "/"}:
        raise ValueError("invalid discovered Edge management URL")
    return edge.management_url.rstrip("/") + "/internal/v1/pairing/complete"
