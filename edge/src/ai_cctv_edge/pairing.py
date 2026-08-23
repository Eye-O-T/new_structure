"""Authenticated LAN discovery and one-time Edge provisioning."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import shutil
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from .auth import BearerAuthenticator, load_tokens
from .config import (
    CAMERA_ID,
    BackupConfig,
    ControlConfig,
    EdgeConfig,
    RecoveryConfig,
    RtspConfig,
    VideoConfig,
    render_toml,
    write_atomic,
)

DISCOVERY_PORT = 37020
DISCOVERY_MESSAGE_TYPE = "AI_CCTV_EDGE_ADVERTISE"
DISCOVERY_VERSION = 1
MAX_DISCOVERY_PACKET = 8192


def _canonical_payload(message: dict[str, object]) -> bytes:
    return json.dumps(
        message,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def load_pairing_key(path: Path) -> str:
    try:
        raw = path.expanduser().resolve().read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("pairing key file cannot be read as UTF-8") from exc
    key = raw.rstrip("\r\n")
    if (
        len(key) < 32
        or key != key.strip()
        or raw not in {key, key + "\n", key + "\r\n"}
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in key)
    ):
        raise ValueError("pairing key must contain at least 32 printable characters")
    return key


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
        raise ValueError("device_id must contain 1..128 characters")
    if not CAMERA_ID.fullmatch(camera_id):
        raise ValueError("invalid camera_id")
    if not 1 <= management_port <= 65535 or not 1 <= recovery_port <= 65535:
        raise ValueError("pairing service ports must be in range 1..65535")
    if management_port == recovery_port:
        raise ValueError("management and recovery ports must differ")
    if any(not isinstance(item, str) for item in supported_profiles):
        raise ValueError("supported profiles may only contain hd and fhd")
    profiles = tuple(dict.fromkeys(supported_profiles))
    if not profiles or any(item not in {"hd", "fhd"} for item in profiles):
        raise ValueError("supported profiles may only contain hd and fhd")
    if (
        len(pairing_key) < 32
        or pairing_key != pairing_key.strip()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in pairing_key)
    ):
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
    result = _canonical_payload({**unsigned, "signature": signature})
    if len(result) > MAX_DISCOVERY_PACKET:
        raise ValueError("discovery advertisement is too large")
    return result


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
    if not 1 <= discovery_port <= 65535:
        raise ValueError("discovery port must be in range 1..65535")
    if interval_seconds <= 0:
        raise ValueError("advertisement interval must be positive")
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


class PairingCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    device_id: str = Field(min_length=1, max_length=128)
    camera_id: str = Field(pattern=CAMERA_ID.pattern)
    central_host: str = Field(min_length=1, max_length=255)
    central_port: int = Field(ge=1, le=65535)
    backup_root: str = Field(
        default="/var/lib/ai-cctv-edge/recordings",
        min_length=1,
        max_length=4096,
    )
    video_profile: Literal["hd", "fhd"] = "hd"
    supported_profiles: list[Literal["hd", "fhd"]] = Field(min_length=1)
    publish_username: str = Field(min_length=1, max_length=256)
    publish_password: str = Field(min_length=16, max_length=1024)


@dataclass
class PairingSession:
    config_path: Path
    pairing_key_file: Path
    device_id: str
    camera_id: str
    management_port: int = 8003
    recovery_port: int = 8002
    completed: threading.Event = field(default_factory=threading.Event)
    apply_lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    @property
    def marker_path(self) -> Path:
        return self.config_path.parent / ".configured"

    def apply(self, request: PairingCompletion) -> EdgeConfig:
        if self.marker_path.exists():
            if self.completed.is_set():
                existing = EdgeConfig.load(self.config_path)
                if (
                    existing.device_id == request.device_id
                    and existing.camera_id == request.camera_id
                ):
                    return existing
            raise HTTPException(status_code=409, detail="Edge is already configured")
        if request.device_id != self.device_id or request.camera_id != self.camera_id:
            raise HTTPException(status_code=409, detail="pairing identity mismatch")
        if request.publish_username != request.camera_id:
            raise HTTPException(
                status_code=422, detail="publish username must match camera_id"
            )
        if (
            request.central_host in {"0.0.0.0", "::"}
            or "://" in request.central_host
            or any(
                character.isspace() or character in "/@?#"
                for character in request.central_host
            )
        ):
            raise HTTPException(status_code=422, detail="invalid central RTSP host")
        if request.publish_password != request.publish_password.strip() or any(
            ord(character) < 0x20 or ord(character) == 0x7F
            for character in request.publish_password
        ):
            raise HTTPException(status_code=422, detail="invalid publish password")
        profiles = tuple(dict.fromkeys(request.supported_profiles))
        if request.video_profile not in profiles:
            raise HTTPException(
                status_code=422, detail="selected profile is not supported"
            )
        backup_root = Path(request.backup_root)
        if not request.backup_root.startswith("/") or "\x00" in request.backup_root:
            raise HTTPException(status_code=422, detail="backup root must be absolute")

        password_file = self.config_path.parent / "publish.password"
        config = EdgeConfig(
            schema_version=1,
            device_id=self.device_id,
            camera_id=self.camera_id,
            video=VideoConfig.from_profile(
                request.video_profile,
                supported_profiles=profiles,
            ),
            rtsp=RtspConfig(
                mode="central_publish",
                central_host=request.central_host,
                central_port=request.central_port,
                username=request.publish_username,
                password_file=password_file,
            ),
            backup=BackupConfig(root=backup_root),
            recovery=RecoveryConfig(
                port=self.recovery_port,
                token_file=self.pairing_key_file,
            ),
            control=ControlConfig(
                port=self.management_port,
                token_file=self.pairing_key_file,
            ),
        )
        config.validate()
        write_atomic(password_file, request.publish_password + "\n", mode=0o640)
        write_atomic(self.config_path, render_toml(config), mode=0o640)
        write_atomic(self.marker_path, "configured\n", mode=0o644)
        self._set_edge_ownership(password_file, self.config_path, self.pairing_key_file)
        self.completed.set()
        return config

    @staticmethod
    def _set_edge_ownership(*paths: Path) -> None:
        if os.name == "nt":
            return
        try:
            import pwd

            account = pwd.getpwnam("ai-cctv-edge")
        except (ImportError, KeyError):
            return
        for path in paths:
            if path.exists():
                shutil.chown(path, user=account.pw_uid, group=account.pw_gid)


def create_pairing_app(session: PairingSession) -> FastAPI:
    authenticate = BearerAuthenticator(load_tokens(session.pairing_key_file))
    app = FastAPI(title="AI_CCTV Edge Pairing", version="0.3.0")

    @app.get("/health/live")
    def health_live():
        return {
            "status": "pairing",
            "device_id": session.device_id,
            "camera_id": session.camera_id,
        }

    @app.put(
        "/internal/v1/pairing/complete",
        dependencies=[Depends(authenticate)],
    )
    def complete(request: PairingCompletion):
        with session.apply_lock:
            config = session.apply(request)
        return {
            "status": "configured",
            "device_id": config.device_id,
            "camera_id": config.camera_id,
            "rtsp_mode": config.rtsp.mode,
        }

    return app
