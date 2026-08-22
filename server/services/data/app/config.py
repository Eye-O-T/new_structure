"""Runtime settings for the Data Service."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from ai_cctv_core.config import load_config


@dataclass(frozen=True, slots=True)
class Settings:
    database_path: Path
    storage_root: Path
    snapshot_root: Path
    backup_root: Path
    internal_token: str
    busy_timeout_ms: int = 5_000
    retention_days: int = 7
    maintenance_interval_seconds: int = 3_600
    event_pre_roll_seconds: int = 5
    event_post_roll_seconds: int = 10
    warning_free_percent: int = 15
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
            initial_admin_username=os.getenv("INITIAL_ADMIN_USERNAME") or None,
            initial_admin_password_hash=(
                os.getenv("INITIAL_ADMIN_PASSWORD_HASH") or None
            ),
            config_path=config_path,
        )

    def prepare_directories(self) -> None:
        if self.retention_days < 1:
            raise ValueError("DATA_RETENTION_DAYS must be at least 1")
        if self.maintenance_interval_seconds < 60:
            raise ValueError("DATA_MAINTENANCE_INTERVAL_SECONDS must be at least 60")
        if self.event_pre_roll_seconds < 0 or self.event_post_roll_seconds < 0:
            raise ValueError("event roll values cannot be negative")
        if not 1 <= self.warning_free_percent <= 99:
            raise ValueError("STORAGE_WARNING_FREE_PERCENT must be in range 1..99")
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.storage_root.mkdir(parents=True, exist_ok=True)
        self.snapshot_root.mkdir(parents=True, exist_ok=True)
        self.backup_root.mkdir(parents=True, exist_ok=True)
