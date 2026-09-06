from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Iterator


DISCOVERY_PORT = 37020
DISCOVERY_MESSAGE_TYPE = "AI_CCTV_EDGE_ADVERTISE"
DISCOVERY_VERSION = 1
MAX_DISCOVERY_PACKET = 8192
CAMERA_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


def utc_timestamp() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def load_secret(path: Path, *, name: str = "secret", minimum: int = 32) -> str:
    try:
        raw = path.expanduser().resolve().read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"{name} file cannot be read as UTF-8") from exc
    value = raw.rstrip("\r\n")
    if (
        len(value) < minimum
        or value != value.strip()
        or raw not in {value, value + "\n", value + "\r\n"}
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in value)
    ):
        raise ValueError(
            f"{name} must contain at least {minimum} printable characters"
        )
    return value


def _canonical_payload(message: dict[str, object]) -> bytes:
    return json.dumps(
        message,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def build_advertisement(
    *,
    device_id: str,
    camera_id: str,
    management_port: int,
    recovery_port: int,
    supported_profiles: tuple[str, ...],
    pairing_key: str,
    sent_at: int | None = None,
    message_id: str | None = None,
) -> bytes:
    if (
        not device_id
        or len(device_id) > 128
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in device_id)
    ):
        raise ValueError("device_id must contain 1..128 printable characters")
    if CAMERA_ID.fullmatch(camera_id) is None:
        raise ValueError("invalid camera_id")
    if not 1 <= management_port <= 65_535 or not 1 <= recovery_port <= 65_535:
        raise ValueError("service ports must be in range 1..65535")
    if management_port == recovery_port:
        raise ValueError("management and recovery ports must differ")
    profiles = tuple(dict.fromkeys(supported_profiles))
    if not profiles or any(item not in {"hd", "fhd"} for item in profiles):
        raise ValueError("supported profiles may only contain hd and fhd")
    if len(pairing_key) < 32:
        raise ValueError("pairing key must contain at least 32 characters")
    identifier = message_id or str(uuid.uuid4())
    try:
        parsed_id = uuid.UUID(identifier)
    except ValueError as exc:
        raise ValueError("message_id must be a UUID") from exc
    if parsed_id.version != 4 or str(parsed_id) != identifier:
        raise ValueError("message_id must be a canonical UUID v4")
    timestamp = int(time.time()) if sent_at is None else sent_at
    if isinstance(timestamp, bool) or not isinstance(timestamp, int):
        raise ValueError("sent_at must be an integer Unix timestamp")
    unsigned: dict[str, object] = {
        "message_type": DISCOVERY_MESSAGE_TYPE,
        "version": DISCOVERY_VERSION,
        "message_id": identifier,
        "sent_at": timestamp,
        "device_id": device_id,
        "camera_id": camera_id,
        "management_port": management_port,
        "recovery_port": recovery_port,
        "supported_profiles": list(profiles),
    }
    signature = hmac.new(
        pairing_key.encode("utf-8"), _canonical_payload(unsigned), hashlib.sha256
    ).hexdigest()
    payload = _canonical_payload({**unsigned, "signature": signature})
    if len(payload) > MAX_DISCOVERY_PACKET:
        raise ValueError("discovery advertisement is too large")
    return payload


def advertise_until_stopped(
    stop: threading.Event,
    *,
    device_id: str,
    camera_id: str,
    management_port: int,
    recovery_port: int,
    supported_profiles: tuple[str, ...],
    pairing_key: str,
    discovery_port: int = DISCOVERY_PORT,
    interval_seconds: float = 1.0,
    destination: str = "255.255.255.255",
) -> None:
    if not 1 <= discovery_port <= 65_535:
        raise ValueError("discovery port must be in range 1..65535")
    if interval_seconds <= 0:
        raise ValueError("discovery interval must be positive")
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as udp_socket:
        udp_socket.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        udp_socket.bind(("0.0.0.0", 0))
        while not stop.is_set():
            payload = build_advertisement(
                device_id=device_id,
                camera_id=camera_id,
                management_port=management_port,
                recovery_port=recovery_port,
                supported_profiles=supported_profiles,
                pairing_key=pairing_key,
            )
            try:
                udp_socket.sendto(payload, (destination, discovery_port))
            except OSError:
                pass
            stop.wait(interval_seconds)


def bearer_matches(authorization: str | None, expected: str) -> bool:
    if not authorization or not authorization.startswith("Bearer "):
        return False
    supplied = authorization[7:]
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def write_atomic(path: Path, text: str, *, mode: int = 0o640) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            os.chmod(path, mode)
    finally:
        temporary.unlink(missing_ok=True)


class EventJournal:
    """Small persistent JSONL journal compatible with the real Edge API."""

    def __init__(self, camera_id: str, root: Path, max_bytes: int = 2 * 1024 * 1024):
        if CAMERA_ID.fullmatch(camera_id) is None:
            raise ValueError("camera_id is invalid")
        self.camera_id = camera_id
        self.root = root
        self.path = root / f"events-{camera_id}.jsonl"
        self.lock_path = root / f"events-{camera_id}.lock"
        self.max_bytes = max_bytes
        self._lock = threading.Lock()

    @contextmanager
    def _file_lock(self, *, exclusive: bool) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+b") as handle:
            unlock: Callable[[], None] | None = None
            try:
                import msvcrt

                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
                mode = msvcrt.LK_LOCK if exclusive else msvcrt.LK_RLCK
                msvcrt.locking(handle.fileno(), mode, 1)

                def unlock_windows() -> None:
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

                unlock = unlock_windows
            except (ImportError, OSError):
                try:
                    import fcntl

                    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                    fcntl.flock(handle.fileno(), operation)

                    def unlock_posix() -> None:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

                    unlock = unlock_posix
                except (ImportError, OSError):
                    pass
            try:
                yield
            finally:
                if unlock is not None:
                    unlock()

    def record(self, event_type: str, **details: Any) -> dict[str, Any]:
        payload = {
            "event_id": uuid.uuid4().hex,
            "event_type": event_type,
            "camera_id": self.camera_id,
            "occurred_at": utc_timestamp(),
            **details,
        }
        encoded = json.dumps(payload, ensure_ascii=False) + "\n"
        self.root.mkdir(parents=True, exist_ok=True)
        with self._lock, self._file_lock(exclusive=True):
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            self._compact_if_needed()
        return payload

    def _compact_if_needed(self) -> None:
        try:
            if self.path.stat().st_size <= self.max_bytes:
                return
            lines = self.path.read_text(encoding="utf-8").splitlines(keepends=True)
        except OSError:
            return
        retained: list[str] = []
        size = 0
        target = self.max_bytes // 2
        for line in reversed(lines):
            line_size = len(line.encode("utf-8"))
            if retained and size + line_size > target:
                break
            retained.append(line)
            size += line_size
        write_atomic(self.path, "".join(reversed(retained)))

    def page(
        self, *, after: str | None = None, limit: int = 100
    ) -> tuple[list[dict[str, Any]], str | None, bool]:
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be in range 1..1000")
        with self._lock, self._file_lock(exclusive=False):
            try:
                lines = self.path.read_text(encoding="utf-8").splitlines()
            except FileNotFoundError:
                lines = []
        all_items: list[dict[str, Any]] = []
        for line in lines:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and item.get("camera_id") == self.camera_id:
                all_items.append(item)
        start = 0
        cursor_expired = False
        if after:
            match = next(
                (
                    index
                    for index, item in enumerate(all_items)
                    if item.get("event_id") == after
                ),
                None,
            )
            if match is None:
                cursor_expired = bool(all_items)
            else:
                start = match + 1
        items = all_items[start : start + limit]
        next_cursor = str(items[-1]["event_id"]) if items else after
        return items, next_cursor, cursor_expired
