from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from .config import CAMERA_ID_PATTERN


Role = Literal["admin", "viewer"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class LoginRequest(StrictModel):
    username: str = Field(min_length=1, max_length=128, pattern=r"^[^/\\\x00-\x1f]+$")
    password: SecretStr = Field(min_length=1, max_length=1024)


class RefreshRequest(StrictModel):
    refresh_token: SecretStr | None = None


class LogoutRequest(StrictModel):
    refresh_token: SecretStr | None = None


class CameraCreate(StrictModel):
    camera_id: str = Field(pattern=CAMERA_ID_PATTERN.pattern)
    name: str = Field(min_length=1, max_length=256)
    stream_path: str | None = Field(default=None, pattern=CAMERA_ID_PATTERN.pattern)
    source_url: str | None = Field(default=None, max_length=2048)
    edge_device_id: str | None = Field(default=None, min_length=1, max_length=256)
    edge_management_url: str | None = Field(default=None, max_length=2048)
    edge_recovery_url: str | None = Field(default=None, max_length=2048)
    edge_auth_token: SecretStr | None = Field(
        default=None, min_length=32, max_length=4096
    )
    enabled: bool = True

    @model_validator(mode="after")
    def complete_edge_metadata(self) -> "CameraCreate":
        values = (
            self.edge_device_id,
            self.edge_management_url,
            self.edge_recovery_url,
            self.edge_auth_token,
        )
        if any(value is not None for value in values) and not all(
            value is not None for value in values
        ):
            raise ValueError(
                "edge_device_id, edge_management_url, edge_recovery_url and "
                "edge_auth_token "
                "must be supplied together"
            )
        return self


class CameraPatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    stream_path: str | None = Field(default=None, pattern=CAMERA_ID_PATTERN.pattern)
    source_url: str | None = Field(default=None, max_length=2048)
    edge_device_id: str | None = Field(default=None, min_length=1, max_length=256)
    edge_management_url: str | None = Field(default=None, max_length=2048)
    edge_recovery_url: str | None = Field(default=None, max_length=2048)
    edge_auth_token: SecretStr | None = Field(
        default=None, min_length=32, max_length=4096
    )
    enabled: bool | None = None


class VideoProfilePatch(StrictModel):
    profile: Literal["hd", "fhd"]


class PublicResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")


class PublishCredentialsResponse(PublicResponse):
    username: str
    password: str


class CameraResponse(PublicResponse):
    id: int | None = None
    camera_id: str = Field(pattern=CAMERA_ID_PATTERN.pattern)
    name: str | None = None
    stream_path: str | None = None
    enabled: bool | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None
    publish_credentials: PublishCredentialsResponse | None = None


class CameraPageResponse(PublicResponse):
    items: list[CameraResponse]
    limit: int = Field(ge=1)
    offset: int = Field(ge=0)


class LiveAuthResponse(PublicResponse):
    method: Literal["cookie"]
    cookie_name: str


class CameraLiveResponse(PublicResponse):
    camera_id: str = Field(pattern=CAMERA_ID_PATTERN.pattern)
    protocol: Literal["hls"]
    url: str
    hls_url: str
    auth: LiveAuthResponse


class VideoProfileResponse(PublicResponse):
    camera_id: str = Field(pattern=CAMERA_ID_PATTERN.pattern)
    current_profile: Literal["hd", "fhd"]
    desired_profile: Literal["hd", "fhd"]
    supported_profiles: list[Literal["hd", "fhd"]] = Field(min_length=1)
    edge_online: bool
    last_error_code: str | None


class CameraStatusResponse(PublicResponse):
    camera_id: str = Field(pattern=CAMERA_ID_PATTERN.pattern)
    online: bool
    cpu_percent: float | None
    memory_percent: float | None
    storage_percent: float | None
    battery_percent: float | None
    power_source: Literal["external", "battery", "unknown"]
    camera_input: Literal["online", "offline", "lost", "unknown"]
    central_connection_status: Literal["online", "offline", "unknown"]
    current_video_profile: Literal["hd", "fhd"]
    last_seen_at: datetime | None
    last_error_code: str | None


class RecordingPlaybackResponse(PublicResponse):
    recording_id: str
    playback_url: str


class UserResponse(PublicResponse):
    id: int | str
    username: str
    role: Role
    is_active: bool
    created_at: datetime | None = None
    updated_at: datetime | None = None


class TokenResponse(PublicResponse):
    access_token: str
    refresh_token: str
    token_type: Literal["bearer"]
    expires_in: int = Field(gt=0)
    user: UserResponse


class UserPageResponse(PublicResponse):
    items: list[UserResponse]
    limit: int | None = Field(default=None, ge=1)
    offset: int | None = Field(default=None, ge=0)


class RecordingResponse(PublicResponse):
    id: int | str
    camera_id: str = Field(pattern=CAMERA_ID_PATTERN.pattern)
    start_time: datetime
    end_time: datetime
    format: Literal["fmp4", "mp4", "mpegts"] | None = None
    codec: str | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    file_size: int | None = Field(default=None, ge=0)
    source: Literal["central", "edge_recovery", "import"] | None = None
    status: (
        Literal["writing", "ready", "missing", "corrupt", "deleting", "deleted"] | None
    ) = None
    checksum: str | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RecordingPageResponse(PublicResponse):
    items: list[RecordingResponse]
    limit: int | None = Field(default=None, ge=1)
    offset: int | None = Field(default=None, ge=0)


class EventResponse(PublicResponse):
    id: int | str
    camera_id: str = Field(pattern=CAMERA_ID_PATTERN.pattern)
    event_type: str
    occurred_at: datetime
    person_id: str | None = None
    track_id: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    recording_segment_id: int | None = None
    recording_segment_ids: list[int] = Field(default_factory=list)
    snapshot_path: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime | None = None


class EventPageResponse(PublicResponse):
    items: list[EventResponse]
    limit: int | None = Field(default=None, ge=1)
    offset: int | None = Field(default=None, ge=0)


class RecoveryJobResponse(PublicResponse):
    id: int
    camera_id: str = Field(pattern=CAMERA_ID_PATTERN.pattern)
    outage_started_at: datetime
    outage_ended_at: datetime | None
    status: Literal[
        "detected",
        "waiting_for_recovery",
        "downloading",
        "indexing",
        "completed",
        "failed",
    ]
    attempt_count: int = Field(ge=0)
    max_attempts: int = Field(gt=0)
    revision: int = Field(default=0, ge=0)
    next_retry_at: datetime | None = None
    last_error: str | None = None
    recovery_summary: dict[str, Any] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class RecoveryJobPageResponse(PublicResponse):
    items: list[RecoveryJobResponse]
    limit: int | None = Field(default=None, ge=1)
    offset: int | None = Field(default=None, ge=0)


class CameraPermissionListResponse(PublicResponse):
    items: list[CameraResponse]


class ServiceStatusResponse(PublicResponse):
    status: str
    version: str | None = None


class SystemStatusResponse(PublicResponse):
    external: ServiceStatusResponse
    data: dict[str, Any]


class UserCreate(StrictModel):
    username: str = Field(min_length=1, max_length=128, pattern=r"^[^/\\\x00-\x1f]+$")
    password: SecretStr = Field(min_length=12, max_length=1024)
    role: Role
    is_active: bool = True


class UserPatch(StrictModel):
    password: SecretStr | None = Field(default=None, min_length=12, max_length=1024)
    role: Role | None = None
    is_active: bool | None = None


class CameraPermissions(StrictModel):
    camera_ids: list[str] = Field(max_length=256)

    def normalized_camera_ids(self) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()
        for camera_id in self.camera_ids:
            if not CAMERA_ID_PATTERN.fullmatch(camera_id):
                raise ValueError("invalid camera ID")
            if camera_id not in seen:
                seen.add(camera_id)
                normalized.append(camera_id)
        return normalized


class AuthVerifyRequest(StrictModel):
    camera_id: str | None = Field(default=None, pattern=CAMERA_ID_PATTERN.pattern)
    resource_type: Literal["camera", "recording", "event"] | None = None
    resource_id: str | None = Field(default=None, min_length=1, max_length=128)


class MediaAuthRequest(BaseModel):
    model_config = ConfigDict(extra="allow", str_strip_whitespace=True)

    action: str = Field(min_length=1, max_length=32)
    protocol: str = Field(default="", max_length=32)
    path: str = Field(default="", max_length=128)
    user: str = Field(default="", max_length=256)
    password: str = Field(default="", max_length=1024)
