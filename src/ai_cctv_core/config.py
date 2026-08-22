"""Versioned configuration schema and atomic YAML persistence."""

from __future__ import annotations

import os
import tempfile
from ipaddress import ip_address
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .identifiers import validate_camera_id, validate_stream_path


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ServerConfig(StrictModel):
    public_http_port: int = Field(default=80, ge=1, le=65535)
    public_https_port: int = Field(default=443, ge=1, le=65535)
    rtsp_bind_address: str = "127.0.0.1"
    rtsp_port: int = Field(default=8554, ge=1, le=65535)
    timezone: str = "Asia/Seoul"

    @field_validator("rtsp_bind_address")
    @classmethod
    def bind_address_is_ip(cls, value: str) -> str:
        ip_address(value)
        return value

    @model_validator(mode="after")
    def ports_must_be_distinct(self) -> "ServerConfig":
        ports = {self.public_http_port, self.public_https_port, self.rtsp_port}
        if len(ports) != 3:
            raise ValueError("public HTTP, HTTPS and RTSP ports must be distinct")
        return self


class RecordingConfig(StrictModel):
    root: str = "./runtime/recordings"
    recovery_root: str = "./runtime/recovered"
    segment_seconds: int = Field(default=60, ge=10, le=300)
    retention_days: int = Field(default=7, ge=1)
    warning_free_percent: int = Field(default=10, ge=1, le=99)
    encryption_at_rest: Literal[False] = False


class InferenceConfig(StrictModel):
    enabled: bool = True
    model_path: str = "./models/default.pt"
    device: str = "auto"
    confidence_threshold: float = Field(default=0.4, ge=0.0, le=1.0)
    analysis_fps: float = Field(default=5.0, gt=0.0, le=30.0)
    disappear_seconds: float = Field(default=3.0, gt=0.0)
    event_pre_roll_seconds: int = Field(default=5, ge=0, le=300)
    event_post_roll_seconds: int = Field(default=10, ge=0, le=300)


class CameraBootstrap(StrictModel):
    camera_id: str
    name: str = Field(min_length=1, max_length=128)
    stream_path: str | None = None
    edge_device_id: str | None = Field(default=None, max_length=128)
    source_url: str | None = None
    enabled: bool = True

    @field_validator("camera_id")
    @classmethod
    def camera_id_is_valid(cls, value: str) -> str:
        return validate_camera_id(value)

    @model_validator(mode="after")
    def normalize_path(self) -> "CameraBootstrap":
        path = self.stream_path or self.camera_id
        validate_stream_path(path)
        if path != self.camera_id:
            raise ValueError("stream_path must equal camera_id in schema version 1")
        self.stream_path = path
        return self


class AppConfig(StrictModel):
    schema_version: Literal[1] = 1
    server: ServerConfig = Field(default_factory=ServerConfig)
    recording: RecordingConfig = Field(default_factory=RecordingConfig)
    inference: InferenceConfig = Field(default_factory=InferenceConfig)
    cameras: list[CameraBootstrap] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def camera_ids_must_be_unique(self) -> "AppConfig":
        ids = [camera.camera_id for camera in self.cameras]
        if len(ids) != len(set(ids)):
            raise ValueError("camera_id values must be unique")
        return self


def load_config(path: str | Path) -> AppConfig:
    with Path(path).open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return AppConfig.model_validate(raw)


def write_config_atomic(config: AppConfig, path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = yaml.safe_dump(
        config.model_dump(mode="json", exclude_none=True),
        allow_unicode=True,
        sort_keys=False,
    )

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
