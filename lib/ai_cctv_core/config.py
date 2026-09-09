# 여러 서비스와 설정 도구가 같은 규칙으로 config.yaml을 읽고 쓰게 하는 공통 모듈이다.
# Pydantic 모델은 자료형뿐 아니라 포트 범위, 중복 ID 등의 조건도 함께 검사한다.

from __future__ import annotations

import os
import tempfile
from ipaddress import ip_address
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .identifiers import validate_camera_id, validate_stream_path


class StrictModel(BaseModel):
    # 알 수 없는 설정 이름을 무시하지 않아 오타가 기본값으로 조용히 대체되는 것을 막는다.
    model_config = ConfigDict(extra="forbid")


# 외부 접속용 포트와 내부 영상 수신 주소를 한 설정으로 검증한다.
class ServerConfig(StrictModel):
    public_http_port: int = Field(default=80, ge=1, le=65535)
    public_https_port: int = Field(default=443, ge=1, le=65535)
    rtsp_bind_address: str = "127.0.0.1"
    rtsp_port: int = Field(default=8554, ge=1, le=65535)
    timezone: str = "Asia/Seoul"

    # 바인딩 주소는 DNS 이름 대신 실제 인터페이스에 지정할 IP만 허용한다.
    @field_validator("rtsp_bind_address")
    @classmethod
    def bind_address_is_ip(cls, value: str) -> str:
        ip_address(value)
        return value

    # 동일 호스트에서 HTTP·HTTPS·RTSP가 같은 포트를 점유하지 않게 한다.
    @model_validator(mode="after")
    def ports_must_be_distinct(self) -> "ServerConfig":
        ports = {self.public_http_port, self.public_https_port, self.rtsp_port}
        if len(ports) != 3:
            raise ValueError("public HTTP, HTTPS and RTSP ports must be distinct")
        return self


# 중앙 녹화와 복구 영상의 저장 위치, 보존 기간 및 용량 경고 기준이다.
class RecordingConfig(StrictModel):
    root: str = "./runtime/recordings"
    recovery_root: str = "./runtime/recovered"
    segment_seconds: int = Field(default=60, ge=10, le=300)
    retention_days: int = Field(default=7, ge=1)
    warning_free_percent: int = Field(default=10, ge=1, le=99)
    encryption_at_rest: Literal[False] = False


class InferenceConfig(StrictModel):
    # confidence는 모델의 탐지 신뢰도 점수이고, analysis_fps는 초당 분석할 프레임 수다.
    # 저장되는 영상의 재생 속도와 분석 빈도는 별개의 설정이다.
    enabled: bool = True
    model_path: str = "./models/default.pt"
    device: str = Field(default="auto", pattern=r"^(?:auto|cpu|cuda(?::[0-9]+)?)$")
    confidence_threshold: float = Field(default=0.4, ge=0.0, le=1.0)
    analysis_fps: float = Field(default=5.0, gt=0.0, le=30.0)
    disappear_seconds: float = Field(default=3.0, gt=0.0)
    event_pre_roll_seconds: int = Field(default=5, ge=0, le=300)
    event_post_roll_seconds: int = Field(default=10, ge=0, le=300)


# 최초 등록할 카메라와 Edge 관리·복구 주소 사이의 연결 정보를 담는다.
class CameraBootstrap(StrictModel):
    camera_id: str
    name: str = Field(min_length=1, max_length=128)
    stream_path: str | None = None
    edge_device_id: str | None = Field(default=None, max_length=128)
    edge_management_url: str | None = Field(default=None, max_length=2048)
    edge_recovery_url: str | None = Field(default=None, max_length=2048)
    source_url: str | None = None
    enabled: bool = True

    @field_validator("camera_id")
    @classmethod
    def camera_id_is_valid(cls, value: str) -> str:
        return validate_camera_id(value)

    # 서버가 호출할 기본 주소이므로 자격 증명·쿼리·상위 경로 이동을 거부한다.
    @field_validator("edge_management_url", "edge_recovery_url")
    @classmethod
    def edge_service_url_is_http(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Edge service URL must be an HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("Edge service URL must not contain credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("Edge service URL must not contain query or fragment")
        if any(part == ".." for part in parsed.path.split("/")):
            raise ValueError("Edge service URL contains an invalid path")
        return value.rstrip("/")

    @model_validator(mode="after")
    def normalize_path(self) -> "CameraBootstrap":
        # 현재 버전은 카메라 ID와 영상 경로를 같게 하여 두 값의 연결 관계를 단순화한다.
        path = self.stream_path or self.camera_id
        validate_stream_path(path)
        if path != self.camera_id:
            raise ValueError("stream_path must equal camera_id in schema version 1")
        self.stream_path = path
        return self


class AppConfig(StrictModel):
    # schema_version은 설정 형식을 바꿀 때 이전 형식과 구분하기 위한 버전 번호다.
    schema_version: Literal[1] = 1
    server: ServerConfig = Field(default_factory=ServerConfig)
    recording: RecordingConfig = Field(default_factory=RecordingConfig)
    inference: InferenceConfig = Field(default_factory=InferenceConfig)
    cameras: list[CameraBootstrap] = Field(default_factory=list, max_length=4)

    # 서로 다른 카메라가 같은 스트림과 저장 경로를 공유하지 않게 한다.
    @model_validator(mode="after")
    def camera_ids_must_be_unique(self) -> "AppConfig":
        ids = [camera.camera_id for camera in self.cameras]
        if len(ids) != len(set(ids)):
            raise ValueError("camera_id values must be unique")
        return self


def load_config(path: str | Path) -> AppConfig:
    # YAML을 읽은 뒤 전체 모델을 검증하므로 잘못된 설정은 서비스 실행 전에 드러난다.
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

    # 같은 디렉터리에 임시 파일을 완성한 뒤 교체한다. 저장 도중 중단되더라도
    # 기존 설정 파일이 일부만 덮어써진 상태로 남지 않게 하기 위한 순서다.
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            # 파이썬 버퍼와 운영체제 버퍼에 남은 내용을 실제 파일에 반영한 뒤 교체한다.
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
