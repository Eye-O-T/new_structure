from __future__ import annotations

import os
import re
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

CAMERA_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class VideoConfig:
    width: int = 1920
    height: int = 1080
    fps: int = 30
    bitrate_kbps: int = 4000
    encoder: str = "x264enc"


@dataclass(frozen=True)
class RtspConfig:
    mode: Literal["central_pull", "central_publish"] = "central_publish"
    central_host: str = "127.0.0.1"
    central_port: int = 8554
    edge_port: int = 8554
    username: str = ""
    password_file: Path = Path("/etc/ai-cctv-edge/publish.password")
    mediamtx_binary: Path = Path("/usr/lib/ai-cctv-edge/mediamtx")


@dataclass(frozen=True)
class BackupConfig:
    root: Path = Path("/var/lib/ai-cctv-edge/recordings")
    segment_seconds: int = 10
    max_bytes: int = 20 * 1024**3
    max_age_hours: int = 24


@dataclass(frozen=True)
class RecoveryConfig:
    bind_host: str = "0.0.0.0"
    port: int = 8002
    token_file: Path = Path("/etc/ai-cctv-edge/recovery.token")


@dataclass(frozen=True)
class EdgeConfig:
    schema_version: int
    device_id: str
    camera_id: str
    video: VideoConfig
    rtsp: RtspConfig
    backup: BackupConfig
    recovery: RecoveryConfig

    @classmethod
    def load(cls, path: str | Path) -> "EdgeConfig":
        with Path(path).open("rb") as handle:
            raw = tomllib.load(handle)
        video = raw.get("video", {})
        rtsp = raw.get("rtsp", {})
        backup = raw.get("backup", {})
        recovery = raw.get("recovery", {})
        config = cls(
            schema_version=int(raw.get("schema_version", 0)),
            device_id=str(raw.get("device_id", "")),
            camera_id=str(raw.get("camera_id", "")),
            video=VideoConfig(
                width=int(video.get("width", 1920)),
                height=int(video.get("height", 1080)),
                fps=int(video.get("fps", 30)),
                bitrate_kbps=int(video.get("bitrate_kbps", 4000)),
                encoder=str(video.get("encoder", "x264enc")),
            ),
            rtsp=RtspConfig(
                mode=str(rtsp.get("mode", "central_publish")),
                central_host=str(rtsp.get("central_host", "127.0.0.1")),
                central_port=int(rtsp.get("central_port", 8554)),
                edge_port=int(rtsp.get("edge_port", 8554)),
                username=str(rtsp.get("username", "")),
                password_file=Path(
                    rtsp.get("password_file", "/etc/ai-cctv-edge/publish.password")
                ),
                mediamtx_binary=Path(
                    rtsp.get("mediamtx_binary", "/usr/lib/ai-cctv-edge/mediamtx")
                ),
            ),
            backup=BackupConfig(
                root=Path(backup.get("root", "/var/lib/ai-cctv-edge/recordings")),
                segment_seconds=int(backup.get("segment_seconds", 10)),
                max_bytes=int(backup.get("max_bytes", 20 * 1024**3)),
                max_age_hours=int(backup.get("max_age_hours", 24)),
            ),
            recovery=RecoveryConfig(
                bind_host=str(recovery.get("bind_host", "0.0.0.0")),
                port=int(recovery.get("port", 8002)),
                token_file=Path(
                    recovery.get("token_file", "/etc/ai-cctv-edge/recovery.token")
                ),
            ),
        )
        config.validate()
        return config

    def validate(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported edge config schema_version")
        if not self.device_id or len(self.device_id) > 128:
            raise ValueError("device_id must contain 1..128 characters")
        if not CAMERA_ID.fullmatch(self.camera_id):
            raise ValueError("invalid camera_id")
        if self.rtsp.mode not in {"central_pull", "central_publish"}:
            raise ValueError("rtsp.mode must be central_pull or central_publish")
        if not 1 <= self.rtsp.central_port <= 65535:
            raise ValueError("invalid central RTSP port")
        if not 1 <= self.rtsp.edge_port <= 65535:
            raise ValueError("invalid edge RTSP port")
        if not 1 <= self.recovery.port <= 65535:
            raise ValueError("invalid recovery port")
        if not (16 <= self.video.width <= 7680 and 16 <= self.video.height <= 4320):
            raise ValueError("unsupported video dimensions")
        if not 1 <= self.video.fps <= 120:
            raise ValueError("video.fps must be in range 1..120")
        if not 100 <= self.video.bitrate_kbps <= 100_000:
            raise ValueError("video.bitrate_kbps must be in range 100..100000")
        if self.backup.segment_seconds != 10:
            raise ValueError(
                "edge backup segment_seconds must be 10 in schema version 1"
            )
        if self.backup.max_bytes <= 0 or self.backup.max_age_hours <= 0:
            raise ValueError("backup limits must be greater than zero")

    @property
    def stream_path(self) -> str:
        return self.camera_id

    @property
    def source_url(self) -> str:
        if self.rtsp.mode == "central_pull":
            return f"rtsp://<edge-address>:{self.rtsp.edge_port}/{self.camera_id}"
        return (
            f"rtsp://{self.rtsp.central_host}:{self.rtsp.central_port}/{self.camera_id}"
        )


def _quote(value: str | Path) -> str:
    escaped = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def render_toml(config: EdgeConfig) -> str:
    return "\n".join(
        [
            f"schema_version = {config.schema_version}",
            f"device_id = {_quote(config.device_id)}",
            f"camera_id = {_quote(config.camera_id)}",
            "",
            "[video]",
            f"width = {config.video.width}",
            f"height = {config.video.height}",
            f"fps = {config.video.fps}",
            f"bitrate_kbps = {config.video.bitrate_kbps}",
            f"encoder = {_quote(config.video.encoder)}",
            "",
            "[rtsp]",
            f"mode = {_quote(config.rtsp.mode)}",
            f"central_host = {_quote(config.rtsp.central_host)}",
            f"central_port = {config.rtsp.central_port}",
            f"edge_port = {config.rtsp.edge_port}",
            f"username = {_quote(config.rtsp.username)}",
            f"password_file = {_quote(config.rtsp.password_file)}",
            f"mediamtx_binary = {_quote(config.rtsp.mediamtx_binary)}",
            "",
            "[backup]",
            f"root = {_quote(config.backup.root)}",
            f"segment_seconds = {config.backup.segment_seconds}",
            f"max_bytes = {config.backup.max_bytes}",
            f"max_age_hours = {config.backup.max_age_hours}",
            "",
            "[recovery]",
            f"bind_host = {_quote(config.recovery.bind_host)}",
            f"port = {config.recovery.port}",
            f"token_file = {_quote(config.recovery.token_file)}",
            "",
        ]
    )


def write_atomic(path: str | Path, text: str, mode: int = 0o640) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(dir=target.parent, prefix=".tmp-")
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
