# Data 시작 시 저장소를 준비하고 비어 있는 계정·카메라 목록에 초기 설정을 반영한다.

from __future__ import annotations

import logging

from ai_cctv_core.config import load_config

from .config import Settings
from .database.repositories import DataRepository

LOGGER = logging.getLogger("ai_cctv.data")


def initialize_runtime(repository: DataRepository, settings: Settings) -> None:
    settings.prepare_directories()

    repository.initialize()

    requeued_jobs = repository.requeue_interrupted_recovery_jobs()

    if requeued_jobs:
        LOGGER.warning("requeued %d interrupted recovery job(s)", requeued_jobs)

    if repository.user_count() == 0:
        username = settings.initial_admin_username
        password_hash = settings.initial_admin_password_hash
        if username and password_hash:
            if not (3 <= len(username) <= 128) or any(
                character in username for character in ("/", "\\", "\x00")
            ):
                raise RuntimeError("INITIAL_ADMIN_USERNAME is invalid")
            if not password_hash.startswith("$argon2"):
                raise RuntimeError(
                    "INITIAL_ADMIN_PASSWORD_HASH must be an Argon2 encoded hash"
                )
            repository.create_user(
                {
                    "username": username,
                    "password_hash": password_hash,
                    "role": "admin",
                    "is_active": True,
                }
            )

    if (
        repository.camera_count() == 0
        and settings.config_path is not None
        and settings.config_path.is_file()
    ):
        bootstrap = load_config(settings.config_path)
        for camera in bootstrap.cameras:
            management_url = getattr(camera, "edge_management_url", None)
            recovery_url = getattr(camera, "edge_recovery_url", None)
            auth_token = (settings.edge_auth_tokens or {}).get(
                camera.edge_device_id or ""
            )
            repository.create_camera(
                {
                    "camera_id": camera.camera_id,
                    "name": camera.name,
                    "stream_path": camera.stream_path,
                    "edge_device_id": camera.edge_device_id,
                    "edge_management_url": management_url,
                    "edge_recovery_url": recovery_url,
                    "edge_auth_token": auth_token,
                    "source_url": camera.source_url,
                    "enabled": camera.enabled,
                    "status": "offline" if camera.enabled else "disabled",
                }
            )

    if settings.config_path is not None and settings.config_path.is_file():
        bootstrap = load_config(settings.config_path)
        for camera in bootstrap.cameras:
            edge_device_id = camera.edge_device_id
            management_url = getattr(camera, "edge_management_url", None)
            recovery_url = getattr(camera, "edge_recovery_url", None)
            auth_token = (settings.edge_auth_tokens or {}).get(edge_device_id or "")
            if edge_device_id and management_url and recovery_url and auth_token:
                repository.put_edge_device(
                    edge_device_id, management_url, recovery_url, auth_token
                )
                stored_camera = repository.get_camera(camera.camera_id)
                if (
                    stored_camera is not None
                    and stored_camera.get("edge_device_id") != edge_device_id
                ):
                    repository.update_camera(
                        camera.camera_id,
                        {"edge_device_id": edge_device_id},
                    )
