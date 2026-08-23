"""Validated request contracts for the internal Data API."""

from __future__ import annotations

from datetime import datetime
from enum import Enum
import re
from typing import Any, Literal
from urllib.parse import urlsplit

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


class VideoProfile(str, Enum):
    hd = "hd"
    fhd = "fhd"


class EventType(str, Enum):
    """Events understood by the central server.

    The two legacy network names remain accepted while older inference
    producers are upgraded. They are stored for input compatibility, but only
    Edge ``central_connection_*`` events define automatic recovery bounds.
    """

    person_detected = "person_detected"
    person_appeared = "person_appeared"
    person_disappeared = "person_disappeared"
    network_failure = "network_failure"
    network_recovery = "network_recovery"
    camera_input_lost = "camera_input_lost"
    camera_input_restored = "camera_input_restored"
    central_connection_lost = "central_connection_lost"
    central_connection_restored = "central_connection_restored"
    inference_stream_lost = "inference_stream_lost"
    inference_stream_restored = "inference_stream_restored"
    external_power_lost = "external_power_lost"
    external_power_restored = "external_power_restored"
    battery_low = "battery_low"
    battery_critical = "battery_critical"
    storage_warning = "storage_warning"
    storage_critical = "storage_critical"
    edge_offline = "edge_offline"
    edge_online = "edge_online"
    video_profile_changed = "video_profile_changed"
    video_profile_change_failed = "video_profile_change_failed"


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


class CameraPermissionsReplace(StrictModel):
    camera_ids: list[str] = Field(max_length=256)

    @field_validator("camera_ids")
    @classmethod
    def validate_camera_ids(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(validate_camera_id(value) for value in values))


class CameraCreate(StrictModel):
    camera_id: str
    name: str = Field(min_length=1, max_length=256)
    stream_path: str
    edge_device_id: str | None = Field(default=None, max_length=256)
    edge_management_url: str | None = Field(default=None, max_length=2048)
    edge_recovery_url: str | None = Field(default=None, max_length=2048)
    edge_auth_token: str | None = Field(default=None, min_length=32, max_length=4096)
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

    @field_validator("edge_management_url", "edge_recovery_url")
    @classmethod
    def validate_edge_url(cls, value: str | None) -> str | None:
        return _management_url(value)

    @model_validator(mode="after")
    def validate_edge_metadata(self) -> "CameraCreate":
        supplied = (
            self.edge_device_id,
            self.edge_management_url,
            self.edge_recovery_url,
            self.edge_auth_token,
        )
        if any(value is not None for value in supplied) and not all(
            value is not None for value in supplied
        ):
            raise ValueError(
                "edge_device_id, edge_management_url, edge_recovery_url and "
                "edge_auth_token "
                "must be supplied together"
            )
        return self


class CameraUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=256)
    stream_path: str | None = None
    edge_device_id: str | None = Field(default=None, max_length=256)
    edge_management_url: str | None = Field(default=None, max_length=2048)
    edge_recovery_url: str | None = Field(default=None, max_length=2048)
    edge_auth_token: str | None = Field(default=None, min_length=32, max_length=4096)
    source_url: str | None = Field(default=None, max_length=2048)
    enabled: bool | None = None
    status: CameraStatus | None = None

    @field_validator("stream_path")
    @classmethod
    def validate_path(cls, value: str | None) -> str | None:
        return None if value is None else validate_stream_path(value)

    @field_validator("edge_management_url", "edge_recovery_url")
    @classmethod
    def validate_edge_url(cls, value: str | None) -> str | None:
        return _management_url(value)

def _management_url(value: str | None) -> str | None:
    if value is None:
        return None
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(part == ".." for part in parsed.path.split("/"))
    ):
        raise ValueError("edge_management_url must be a credential-free HTTP(S) URL")
    return value.rstrip("/")


class CameraStatusUpdate(StrictModel):
    status: CameraStatus


class CameraPublishCredentialPut(StrictModel):
    username: str = Field(min_length=1, max_length=256)
    password_hash: str = Field(min_length=16, max_length=1024)


class EdgeDevicePut(StrictModel):
    management_url: str
    recovery_url: str
    auth_token: str = Field(min_length=32, max_length=4096)

    @field_validator("management_url")
    @classmethod
    def validate_management_url(cls, value: str) -> str:
        validated = _management_url(value)
        if validated is None:  # pragma: no cover - pydantic already rejects it
            raise ValueError("management_url is required")
        return validated

    @field_validator("recovery_url")
    @classmethod
    def validate_recovery_url(cls, value: str) -> str:
        validated = _management_url(value)
        if validated is None:  # pragma: no cover
            raise ValueError("recovery_url is required")
        return validated


class VideoProfileStatePatch(StrictModel):
    desired_profile: VideoProfile | None = None
    current_profile: VideoProfile | None = None
    supported_profiles: list[VideoProfile] | None = Field(
        default=None, min_length=1, max_length=2
    )
    encoder: str | None = Field(default=None, min_length=1, max_length=128)
    last_error_code: str | None = Field(default=None, max_length=128)

    @field_validator("supported_profiles")
    @classmethod
    def normalize_profiles(
        cls, value: list[VideoProfile] | None
    ) -> list[VideoProfile] | None:
        if value is None:
            return None
        return list(dict.fromkeys(value))

    @model_validator(mode="after")
    def require_change(self) -> "VideoProfileStatePatch":
        if not self.model_fields_set:
            raise ValueError("at least one video profile field is required")
        return self


class CameraRuntimeStatusPut(StrictModel):
    online: bool
    cpu_percent: float | None = Field(default=None, ge=0, le=100)
    memory_percent: float | None = Field(default=None, ge=0, le=100)
    storage_percent: float | None = Field(default=None, ge=0, le=100)
    battery_percent: float | None = Field(default=None, ge=0, le=100)
    power_source: Literal["external", "battery", "unknown"] = "unknown"
    camera_input: Literal["online", "offline", "lost", "unknown"] = "unknown"
    central_connection_status: Literal["online", "offline", "unknown"] = "unknown"
    current_video_profile: VideoProfile | None = None
    last_seen_at: datetime | None = None
    last_error_code: str | None = Field(default=None, max_length=128)
    event_cursor: str | None = Field(default=None, max_length=256)

    @field_validator("last_seen_at")
    @classmethod
    def validate_seen_at(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _utc(value)


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
    event_type: EventType | str = Field(min_length=1, max_length=128)
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
    edge_event_id: str | None = Field(default=None, min_length=1, max_length=256)

    @field_validator("camera_id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return validate_camera_id(value)

    @field_validator("occurred_at")
    @classmethod
    def validate_time(cls, value: datetime) -> datetime:
        return _utc(value)

    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, value: EventType | str) -> EventType | str:
        raw = value.value if isinstance(value, EventType) else value
        if re.fullmatch(r"[a-z][a-z0-9_]{0,127}", raw) is None:
            raise ValueError("event_type must be a lowercase identifier")
        return value


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
