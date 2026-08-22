from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr

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
    enabled: bool = True


class CameraPatch(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=128)
    stream_path: str | None = Field(default=None, pattern=CAMERA_ID_PATTERN.pattern)
    source_url: str | None = Field(default=None, max_length=2048)
    enabled: bool | None = None


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
    path: str = Field(default="", max_length=128)
    user: str = Field(default="", max_length=256)
    password: str = Field(default="", max_length=1024)
