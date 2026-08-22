"""Validated request contracts for the internal Data API."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Any

from ai_cctv_core.identifiers import validate_camera_id, validate_stream_path
from ai_cctv_core.time import parse_utc
from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Role(str, Enum):
    admin = "admin"
    viewer = "viewer"


class CameraStatus(str, Enum):
    online = "online"
    offline = "offline"
    degraded = "degraded"
    disabled = "disabled"


class SegmentFormat(str, Enum):
    fmp4 = "fmp4"
    mp4 = "mp4"
    mpegts = "mpegts"


class SegmentSource(str, Enum):
    central = "central"
    edge_recovery = "edge_recovery"
    import_ = "import"


class SegmentStatus(str, Enum):
    writing = "writing"
    ready = "ready"
    missing = "missing"
    corrupt = "corrupt"
    deleting = "deleting"
    deleted = "deleted"


def _utc(value: datetime) -> datetime:
    return parse_utc(value)


class UserCreate(StrictModel):
    username: str = Field(min_length=1, max_length=128)
    password_hash: str = Field(min_length=1, max_length=1024)
    role: Role
    is_active: bool = True


class UserUpdate(StrictModel):
    username: str | None = Field(default=None, min_length=1, max_length=128)
    password_hash: str | None = Field(default=None, min_length=1, max_length=1024)
    role: Role | None = None
    is_active: bool | None = None


class CameraCreate(StrictModel):
    camera_id: str
    name: str = Field(min_length=1, max_length=256)
    stream_path: str
    edge_device_id: str | None = Field(default=None, max_length=256)
    source_url: str | None = Field(default=None, max_length=2048)
    enabled: bool = True
    status: CameraStatus = CameraStatus.offline

    @field_validator("camera_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return validate_camera_id(value)

    @field_validator("stream_path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        return validate_stream_path(value)


class CameraUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=256)
    stream_path: str | None = None
    edge_device_id: str | None = Field(default=None, max_length=256)
    source_url: str | None = Field(default=None, max_length=2048)
    enabled: bool | None = None
    status: CameraStatus | None = None

    @field_validator("stream_path")
    @classmethod
    def validate_path(cls, value: str | None) -> str | None:
        return None if value is None else validate_stream_path(value)


class CameraStatusUpdate(StrictModel):
    status: CameraStatus


class CameraPublishCredentialPut(StrictModel):
    username: str = Field(min_length=1, max_length=256)
    password_hash: str = Field(min_length=16, max_length=1024)


class RecordingSegmentCreate(StrictModel):
    camera_id: str
    start_time: datetime
    end_time: datetime
    relative_path: str = Field(min_length=1, max_length=4096)
    format: SegmentFormat
    codec: str = Field(default="h264", min_length=1, max_length=64)
    duration_ms: int | None = Field(default=None, ge=0)
    file_size: int | None = Field(default=None, ge=0)
    source: SegmentSource = SegmentSource.central
    status: SegmentStatus = SegmentStatus.ready
    checksum: str | None = Field(default=None, min_length=1, max_length=256)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("camera_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return validate_camera_id(value)

    @field_validator("start_time", "end_time")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @model_validator(mode="after")
    def validate_interval(self) -> "RecordingSegmentCreate":
        if self.end_time <= self.start_time:
            raise ValueError("end_time must be later than start_time")
        actual_duration = int((self.end_time - self.start_time).total_seconds() * 1000)
        duration_mismatch = (
            self.duration_ms is not None
            and abs(self.duration_ms - actual_duration) > 1_000
        )
        if duration_mismatch:
            raise ValueError("duration_ms does not match the timestamp interval")
        return self


class EventCreate(StrictModel):
    camera_id: str
    event_type: str = Field(min_length=1, max_length=128)
    occurred_at: datetime
    person_id: str | None = Field(default=None, max_length=256)
    track_id: str | None = Field(default=None, max_length=256)
    confidence: float | None = Field(default=None, ge=0, le=1)
    recording_segment_id: int | None = Field(default=None, ge=1)
    recording_segment_ids: list[int] = Field(default_factory=list)
    snapshot_path: str | None = Field(default=None, max_length=4096)
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("metadata", "metadata_json"),
    )

    @field_validator("camera_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return validate_camera_id(value)

    @field_validator("occurred_at")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return _utc(value)


class RefreshTokenCreate(StrictModel):
    user_id: int = Field(ge=1)
    jti: str = Field(min_length=1, max_length=256)
    token_hash: str = Field(min_length=1, max_length=1024)
    expires_at: datetime
    family_id: str | None = Field(default=None, max_length=256)
    rotated_from_jti: str | None = Field(default=None, max_length=256)

    @field_validator("expires_at")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return _utc(value)


class RevokedTokenPut(StrictModel):
    user_id: int | None = Field(default=None, ge=1)
    expires_at: datetime
    reason: str | None = Field(default=None, max_length=512)

    @field_validator("expires_at")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return _utc(value)


class RetentionRequest(StrictModel):
    retention_days: int | None = Field(default=None, ge=1)
    before: datetime | None = None
    dry_run: bool = True

    @field_validator("before")
    @classmethod
    def validate_time(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)

    @model_validator(mode="after")
    def validate_cutoff(self) -> "RetentionRequest":
        if self.retention_days is None and self.before is None:
            raise ValueError("retention_days or before is required")
        if self.retention_days is not None and self.before is not None:
            raise ValueError("retention_days and before are mutually exclusive")
        return self


class BackupRequest(StrictModel):
    filename: str | None = Field(default=None, min_length=1, max_length=255)
