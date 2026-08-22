from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ai_cctv_core.config import AppConfig, load_config


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    data_service_url: str
    internal_service_token: str
    rtsp_base_url: str
    snapshots_root: Path
    model_path: Path
    device: str
    confidence: float
    analysis_fps: float
    disappear_seconds: float
    refresh_seconds: float
    inference_enabled: bool

    @classmethod
    def from_env(cls) -> "Settings":
        config: AppConfig | None = None
        config_path = os.getenv("AI_CCTV_CONFIG_FILE")
        if config_path and Path(config_path).is_file():
            config = load_config(config_path)
        inference = config.inference if config is not None else None

        def configured(name: str, fallback: object) -> str:
            return os.getenv(name, str(fallback))

        return cls(
            data_service_url=os.getenv(
                "DATA_SERVICE_URL", "http://nginx:8080/internal/data/v1"
            ).rstrip("/"),
            internal_service_token=os.getenv("INTERNAL_SERVICE_TOKEN", ""),
            rtsp_base_url=os.getenv(
                "MEDIAMTX_RTSP_BASE_URL", "rtsp://mediamtx:8554"
            ).rstrip("/"),
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
        )

    def validate(self) -> None:
        if not self.internal_service_token:
            raise ValueError("INTERNAL_SERVICE_TOKEN is required")
        if not 0 <= self.confidence <= 1:
            raise ValueError("INFERENCE_CONFIDENCE must be in range 0..1")
        if self.analysis_fps <= 0:
            raise ValueError("ANALYSIS_FPS must be greater than zero")
        if self.disappear_seconds <= 0:
            raise ValueError("DISAPPEAR_SECONDS must be greater than zero")
