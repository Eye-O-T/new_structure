"""Shared, dependency-light contracts used by AI_CCTV tools and services."""

from .config import AppConfig, CameraBootstrap, load_config, write_config_atomic
from .identifiers import CAMERA_ID_PATTERN, validate_camera_id, validate_stream_path
from .time import format_utc, parse_utc, utc_now

__all__ = [
    "AppConfig",
    "CAMERA_ID_PATTERN",
    "CameraBootstrap",
    "format_utc",
    "load_config",
    "parse_utc",
    "utc_now",
    "validate_camera_id",
    "validate_stream_path",
    "write_config_atomic",
]
