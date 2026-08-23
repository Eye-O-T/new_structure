"""Runtime settings for the Data Service."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ai_cctv_core.config import load_config


@dataclass(frozen=True, slots=True)
class Settings:
    database_path: Path
    storage_root: Path
    snapshot_root: Path
    backup_root: Path
    internal_token: str
    data_external_token: str | None = None
    data_inference_token: str | None = None
    data_media_token: str | None = None
    data_recovery_token: str | None = None
    busy_timeout_ms: int = 5_000
    retention_days: int = 7
    maintenance_interval_seconds: int = 3_600
    event_pre_roll_seconds: int = 5
    event_post_roll_seconds: int = 10
    warning_free_percent: int = 15
    recovery_poll_interval_seconds: float = 5.0
    recovery_max_attempts: int = 3
    recovery_retry_base_seconds: int = 30
    recovery_settle_seconds: int = 15
    recovery_timeout_seconds: float = 30.0
    central_recording_segment_seconds: int = 60
    recovery_data_base_url: str = "http://127.0.0.1:8000/internal/v1"
    edge_auth_tokens: Mapping[str, str] | None = None
    initial_admin_username: str | None = None
    initial_admin_password_hash: str | None = None
    config_path: Path | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        config_path = (
            Path(os.environ["AI_CCTV_CONFIG_FILE"])
            if os.getenv("AI_CCTV_CONFIG_FILE")
            else None
        )
        shared_config = (
            load_config(config_path)
            if config_path is not None and config_path.is_file()
            else None
        )
        database_path = Path(
            os.getenv("DATA_DATABASE_PATH")
            or os.getenv("AI_CCTV_DATABASE_PATH")
            or "/data/database/ai_cctv.db"
        )
        storage_root = Path(
            os.getenv("DATA_STORAGE_ROOT")
            or os.getenv("RECORDINGS_ROOT")
            or "/data/recordings"
        )
        snapshot_root = Path(
            os.getenv("DATA_SNAPSHOT_ROOT")
            or os.getenv("SNAPSHOTS_ROOT")
            or "/data/snapshots"
        )
        backup_root = Path(os.getenv("DATA_BACKUP_ROOT", "/data/database/backups"))
        try:
            edge_auth_tokens = json.loads(os.getenv("EDGE_AUTH_TOKENS_JSON", "{}"))
        except json.JSONDecodeError as exc:
            raise ValueError("EDGE_AUTH_TOKENS_JSON must be valid JSON") from exc
        if not isinstance(edge_auth_tokens, dict) or any(
            not isinstance(key, str)
            or not isinstance(value, str)
            or len(value) < 32
            for key, value in edge_auth_tokens.items()
        ):
            raise ValueError(
                "EDGE_AUTH_TOKENS_JSON must map Edge device IDs to tokens"
            )
        return cls(
            database_path=database_path,
            storage_root=storage_root,
            snapshot_root=snapshot_root,
            backup_root=backup_root,
            internal_token=(
                os.getenv("DATA_INTERNAL_TOKEN")
                or os.getenv("INTERNAL_SERVICE_TOKEN")
                or ""
            ),
            data_external_token=os.getenv("DATA_EXTERNAL_TOKEN") or None,
            data_inference_token=os.getenv("DATA_INFERENCE_TOKEN") or None,
            data_media_token=os.getenv("DATA_MEDIA_TOKEN") or None,
            data_recovery_token=os.getenv("DATA_RECOVERY_TOKEN") or None,
            busy_timeout_ms=int(os.getenv("DATA_SQLITE_BUSY_TIMEOUT_MS", "5000")),
            retention_days=int(
                os.getenv(
                    "DATA_RETENTION_DAYS",
                    str(shared_config.recording.retention_days if shared_config else 7),
                )
            ),
            maintenance_interval_seconds=int(
                os.getenv("DATA_MAINTENANCE_INTERVAL_SECONDS", "3600")
            ),
            event_pre_roll_seconds=int(
                os.getenv(
                    "EVENT_PRE_ROLL_SECONDS",
                    str(
                        shared_config.inference.event_pre_roll_seconds
                        if shared_config
                        else 5
                    ),
                )
            ),
            event_post_roll_seconds=int(
                os.getenv(
                    "EVENT_POST_ROLL_SECONDS",
                    str(
                        shared_config.inference.event_post_roll_seconds
                        if shared_config
                        else 10
                    ),
                )
            ),
            warning_free_percent=int(
                os.getenv(
                    "STORAGE_WARNING_FREE_PERCENT",
                    str(
                        shared_config.recording.warning_free_percent
                        if shared_config
                        else 15
                    ),
                )
            ),
            recovery_poll_interval_seconds=float(
                os.getenv("RECOVERY_POLL_INTERVAL_SECONDS", "5")
            ),
            recovery_max_attempts=int(os.getenv("RECOVERY_MAX_ATTEMPTS", "3")),
            recovery_retry_base_seconds=int(
                os.getenv("RECOVERY_RETRY_BASE_SECONDS", "30")
            ),
            recovery_settle_seconds=int(
                os.getenv("RECOVERY_SETTLE_SECONDS", "15")
            ),
            recovery_timeout_seconds=float(
                os.getenv("RECOVERY_TIMEOUT_SECONDS", "30")
            ),
            central_recording_segment_seconds=int(
                os.getenv(
                    "CENTRAL_RECORDING_SEGMENT_SECONDS",
                    str(shared_config.recording.segment_seconds if shared_config else 60),
                )
            ),
            recovery_data_base_url=os.getenv(
                "RECOVERY_DATA_BASE_URL",
                "http://127.0.0.1:8000/internal/v1",
            ).rstrip("/"),
            edge_auth_tokens=edge_auth_tokens,
            initial_admin_username=os.getenv("INITIAL_ADMIN_USERNAME") or None,
            initial_admin_password_hash=(
                os.getenv("INITIAL_ADMIN_PASSWORD_HASH") or None
            ),
            config_path=config_path,
        )

    def data_api_tokens(self) -> dict[str, str]:
        """Return effective Data API tokens, including the legacy fallback."""

        scoped_tokens = {
            "external": self.data_external_token,
            "inference": self.data_inference_token,
            "media": self.data_media_token,
            "recovery": self.data_recovery_token,
        }
        if any(scoped_tokens.values()):
            return {
                scope: token or "" for scope, token in scoped_tokens.items()
            }
        return {
            scope: self.internal_token for scope in scoped_tokens
        }

    def prepare_directories(self) -> None:
        scoped_tokens = (
            self.data_external_token,
            self.data_inference_token,
            self.data_media_token,
            self.data_recovery_token,
        )
        if any(scoped_tokens):
            if not all(scoped_tokens):
                raise ValueError(
                    "DATA_EXTERNAL_TOKEN, DATA_INFERENCE_TOKEN, DATA_MEDIA_TOKEN, "
                    "and DATA_RECOVERY_TOKEN must be configured together"
                )
            normalized_tokens = [str(token) for token in scoped_tokens]
            if any(len(token) < 32 for token in normalized_tokens):
                raise ValueError("scoped Data API tokens must contain 32+ characters")
            if len(set(normalized_tokens)) != len(normalized_tokens):
                raise ValueError("scoped Data API tokens must be distinct")
        elif len(self.internal_token) < 16:
            raise ValueError(
                "legacy DATA_INTERNAL_TOKEN or INTERNAL_SERVICE_TOKEN must contain "
                "at least 16 characters"
            )
        if self.retention_days < 1:
            raise ValueError("DATA_RETENTION_DAYS must be at least 1")
        if self.maintenance_interval_seconds < 60:
            raise ValueError("DATA_MAINTENANCE_INTERVAL_SECONDS must be at least 60")
        if self.event_pre_roll_seconds < 0 or self.event_post_roll_seconds < 0:
            raise ValueError("event roll values cannot be negative")
        if not 1 <= self.warning_free_percent <= 99:
            raise ValueError("STORAGE_WARNING_FREE_PERCENT must be in range 1..99")
        if self.recovery_poll_interval_seconds <= 0:
            raise ValueError("RECOVERY_POLL_INTERVAL_SECONDS must be greater than zero")
        if self.recovery_max_attempts < 1:
            raise ValueError("RECOVERY_MAX_ATTEMPTS must be at least 1")
        if self.recovery_retry_base_seconds < 1:
            raise ValueError("RECOVERY_RETRY_BASE_SECONDS must be at least 1")
        if self.recovery_settle_seconds < 0:
            raise ValueError("RECOVERY_SETTLE_SECONDS cannot be negative")
        if self.recovery_timeout_seconds <= 0:
            raise ValueError("RECOVERY_TIMEOUT_SECONDS must be greater than zero")
        if not 10 <= self.central_recording_segment_seconds <= 300:
            raise ValueError(
                "CENTRAL_RECORDING_SEGMENT_SECONDS must be in range 10..300"
            )
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.snapshot_root.mkdir(parents=True, exist_ok=True)
        self.backup_root.mkdir(parents=True, exist_ok=True)
