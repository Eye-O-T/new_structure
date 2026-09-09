# 설정 파일의 기본값 위에 환경변수 값을 적용해 컨테이너에서 사용할 설정을 만든다.
# 배포 환경마다 달라지는 주소·비밀번호는 코드에 직접 적지 않고 환경변수로 받는다.
from __future__ import annotations

import os
import re
import math
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlsplit, urlunsplit

from ai_cctv_core.config import AppConfig, load_config


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
        raise ValueError(f"{name} must be a boolean")
    return normalized in {"1", "true", "yes", "on"}


# 영상 읽기·탐지·식별에 필요한 불변 실행 설정이며 탐지와 식별의 내부 토큰을 구분한다.
@dataclass(frozen=True)
class Settings:
    data_service_url: str
    internal_service_token: str
    rtsp_base_url: str
    media_read_username: str
    media_read_password: str
    snapshots_root: Path
    model_path: Path
    device: str
    confidence: float
    analysis_fps: float
    disappear_seconds: float
    refresh_seconds: float
    inference_enabled: bool
    detection_plugin: str = (
        "server.services.preprocessing.processors.detection.yolo:YoloTracker"
    )
    identity_plugin: str = (
        "server.services.preprocessing.processors.identity:OsNetIdentity"
    )
    identity_token: str = ""
    capture_timeout_seconds: float = 5.0
    model_retry_seconds: float = 30.0
    shutdown_timeout_seconds: float = 15.0
    event_outbox_max_pending: int = 10000
    event_outbox_max_bytes: int = 64 * 1024 * 1024
    detection_timeout_seconds: float = 10.0
    detection_startup_seconds: float = 30.0
    observation_window_seconds: float = 0.0
    observation_buffer_max_bytes: int = 32 * 1024 * 1024

    # 공통 설정의 추론 값을 기본으로 읽고 환경변수와 서비스별 기본값을 적용한다.
    @classmethod
    def from_env(cls) -> "Settings":
        config: AppConfig | None = None
        config_path = os.getenv("AI_CCTV_CONFIG_FILE")
        if config_path:
            config = load_config(config_path)
        inference = config.inference if config is not None else None

        def configured(name: str, fallback: object) -> str:
            # 우선순위는 환경변수, config.yaml 값, 코드 기본값 순서다.
            return os.getenv(name, str(fallback))

        return cls(
            data_service_url=os.getenv(
                "DATA_SERVICE_URL", "http://nginx:8080/internal/data/v1"
            ).rstrip("/"),
            internal_service_token=(
                os.getenv("DATA_INFERENCE_TOKEN")
                or os.getenv("INTERNAL_SERVICE_TOKEN")
                or ""
            ),
            rtsp_base_url=os.getenv(
                "MEDIAMTX_RTSP_BASE_URL", "rtsp://mediamtx:8554"
            ).rstrip("/"),
            media_read_username=os.getenv("MEDIA_READ_USERNAME", ""),
            media_read_password=os.getenv("MEDIA_READ_PASSWORD", ""),
            snapshots_root=Path(os.getenv("SNAPSHOTS_ROOT", "/snapshots")),
            model_path=Path(
                configured(
                    "MODEL_PATH",
                    inference.model_path if inference else "/models/default.pt",
                )
            ),
            device=configured(
                "INFERENCE_DEVICE", inference.device if inference else "auto"
            ),
            confidence=float(
                configured(
                    "INFERENCE_CONFIDENCE",
                    inference.confidence_threshold if inference else 0.4,
                )
            ),
            analysis_fps=float(
                configured("ANALYSIS_FPS", inference.analysis_fps if inference else 5)
            ),
            disappear_seconds=float(
                configured(
                    "DISAPPEAR_SECONDS",
                    inference.disappear_seconds if inference else 3,
                )
            ),
            refresh_seconds=float(os.getenv("CAMERA_REFRESH_SECONDS", "15")),
            inference_enabled=_bool(
                "INFERENCE_ENABLED", inference.enabled if inference else True
            ),
            detection_plugin=os.getenv(
                "DETECTION_PLUGIN",
                "server.services.preprocessing.processors.detection.yolo:YoloTracker",
            ),
            identity_plugin=os.getenv(
                "IDENTITY_PLUGIN",
                "server.services.preprocessing.processors.identity:OsNetIdentity",
            ),
            identity_token=os.getenv("DATA_IDENTITY_TOKEN", ""),
            capture_timeout_seconds=float(os.getenv("RTSP_TIMEOUT_SECONDS", "5")),
            model_retry_seconds=float(os.getenv("MODEL_RETRY_SECONDS", "30")),
            shutdown_timeout_seconds=float(
                os.getenv("DETECTION_SHUTDOWN_SECONDS", "15")
            ),
            event_outbox_max_pending=int(
                os.getenv("EVENT_OUTBOX_MAX_PENDING", "10000")
            ),
            event_outbox_max_bytes=int(os.getenv("EVENT_OUTBOX_MAX_BYTES", "67108864")),
            detection_timeout_seconds=float(
                os.getenv("DETECTION_MODEL_TIMEOUT_SECONDS", "10")
            ),
            detection_startup_seconds=float(
                os.getenv("DETECTION_STARTUP_TIMEOUT_SECONDS", "30")
            ),
            observation_window_seconds=float(
                os.getenv("OBSERVATION_WINDOW_SECONDS", "1")
            ),
            observation_buffer_max_bytes=int(
                os.getenv("OBSERVATION_BUFFER_MAX_BYTES", "33554432")
            ),
        )

    def validate(self) -> None:
        # 연결 후 오류가 반복되기 전에 주소 형식, 인증 정보와 수치 범위를 확인한다.
        if not self.internal_service_token:
            raise ValueError(
                "DATA_INFERENCE_TOKEN or legacy INTERNAL_SERVICE_TOKEN is required"
            )
        if any(character.isspace() for character in self.internal_service_token):
            raise ValueError("Data token must not contain whitespace")
        data_url = urlsplit(self.data_service_url)
        if (
            data_url.scheme not in {"http", "https"}
            or not data_url.hostname
            or data_url.username is not None
            or data_url.password is not None
            or data_url.query
            or data_url.fragment
        ):
            raise ValueError("DATA_SERVICE_URL must be a credential-free HTTP(S) URL")
        data_url.port
        parsed = urlsplit(self.rtsp_base_url)
        if parsed.scheme not in {"rtsp", "rtsps"} or not parsed.hostname:
            raise ValueError("MEDIAMTX_RTSP_BASE_URL must be an RTSP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError(
                "MEDIAMTX_RTSP_BASE_URL must not contain embedded credentials"
            )
        if parsed.query or parsed.fragment:
            raise ValueError(
                "MEDIAMTX_RTSP_BASE_URL must not contain a query or fragment"
            )
        parsed.port
        if any(part in {".", ".."} for part in parsed.path.split("/")):
            raise ValueError("MEDIAMTX_RTSP_BASE_URL contains an invalid path")
        if not self.media_read_username or any(
            character in self.media_read_username for character in "\x00\r\n"
        ):
            raise ValueError("MEDIA_READ_USERNAME must be a non-empty single line")
        if len(self.media_read_password) < 32:
            raise ValueError("MEDIA_READ_PASSWORD must contain at least 32 characters")
        if any(character in self.media_read_password for character in "\x00\r\n"):
            raise ValueError("MEDIA_READ_PASSWORD must be a single line")
        if re.fullmatch(r"(?:auto|cpu|cuda(?::[0-9]+)?)", self.device) is None:
            raise ValueError(
                "INFERENCE_DEVICE must be auto, cpu, cuda, or cuda:<index>"
            )
        if not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("INFERENCE_CONFIDENCE must be in range 0..1")
        for name, value, maximum in (
            ("ANALYSIS_FPS", self.analysis_fps, 60),
            ("DISAPPEAR_SECONDS", self.disappear_seconds, 3600),
            ("CAMERA_REFRESH_SECONDS", self.refresh_seconds, 3600),
            ("RTSP_TIMEOUT_SECONDS", self.capture_timeout_seconds, 30),
            ("MODEL_RETRY_SECONDS", self.model_retry_seconds, 3600),
            ("DETECTION_SHUTDOWN_SECONDS", self.shutdown_timeout_seconds, 60),
            ("DETECTION_MODEL_TIMEOUT_SECONDS", self.detection_timeout_seconds, 60),
            ("DETECTION_STARTUP_TIMEOUT_SECONDS", self.detection_startup_seconds, 120),
        ):
            if not math.isfinite(value) or not 0 < value <= maximum:
                raise ValueError(f"{name} must be finite and in range 0..{maximum}")
        if not 1 <= self.event_outbox_max_pending <= 100000:
            raise ValueError("EVENT_OUTBOX_MAX_PENDING must be in range 1..100000")
        if not 1024 <= self.event_outbox_max_bytes <= 1024 * 1024 * 1024:
            raise ValueError("EVENT_OUTBOX_MAX_BYTES must be in range 1024..1073741824")
        if (
            not math.isfinite(self.observation_window_seconds)
            or not 0 <= self.observation_window_seconds <= 5
        ):
            raise ValueError("OBSERVATION_WINDOW_SECONDS must be in range 0..5")
        if not 256 * 1024 <= self.observation_buffer_max_bytes <= 512 * 1024 * 1024:
            raise ValueError(
                "OBSERVATION_BUFFER_MAX_BYTES must be in range 262144..536870912"
            )

    def rtsp_source_url(self, stream_path: str) -> str:
        """인증 정보를 인코딩한 RTSP URL을 만든다. 반환값은 로그에 남기지 않는다."""

        parsed = urlsplit(self.rtsp_base_url)
        normalized_path = stream_path
        if (
            not normalized_path
            or normalized_path.startswith("/")
            or "\\" in normalized_path
            or any(ord(character) < 0x20 for character in normalized_path)
            or any(part in {"", ".", ".."} for part in normalized_path.split("/"))
        ):
            raise ValueError(
                "RTSP stream path must be a relative path without traversal"
            )
        # URL에서 의미를 가지는 특수 문자를 인코딩해 경로와 인증 정보가 섞이지 않게 한다.
        escaped_path = quote(normalized_path, safe="/-._~")
        base_path = parsed.path.rstrip("/")
        path = f"{base_path}/{escaped_path}"
        userinfo = (
            f"{quote(self.media_read_username, safe='')}:"
            f"{quote(self.media_read_password, safe='')}@"
        )
        return urlunsplit((parsed.scheme, f"{userinfo}{parsed.netloc}", path, "", ""))
