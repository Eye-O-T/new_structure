from __future__ import annotations

import errno
import json
import os
import re
import secrets
import shutil
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path, PurePosixPath

from argon2 import PasswordHasher

from ai_cctv_core.config import AppConfig, CameraBootstrap, write_config_atomic

from .model_manager import validate_custom_model

SAFE_ENV = re.compile(r"^[A-Za-z0-9_./:@+-]+$")


@dataclass(frozen=True)
class InstallRequest:
    data_root: Path
    server_dir: Path
    admin_username: str
    admin_password: str
    model_path: Path
    cameras: list[CameraBootstrap]
    public_http_port: int = 80
    public_https_port: int = 443
    public_bind_address: str = "127.0.0.1"
    rtsp_bind_address: str = "0.0.0.0"
    rtsp_port: int = 8554
    timezone: str = "Asia/Seoul"


@dataclass(frozen=True)
class InstallResult:
    config_path: Path
    secrets_path: Path
    compose_env_path: Path
    camera_credentials_path: Path
    camera_credentials: dict[str, dict[str, str]]


def _dotenv(value: str | Path | int) -> str:
    text = str(value)
    if "\n" in text or "\r" in text:
        raise ValueError("environment values must be single-line")
    if SAFE_ENV.fullmatch(text):
        return text
    # Compose interpolates `$NAME` in unquoted and double-quoted env-file
    # values. Single quotes preserve Argon2 hashes and other secret values
    # literally; Compose represents an embedded quote as \'.
    return "'" + text.replace("'", "\\'") + "'"


def _write_atomic(path: Path, content: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _copy_atomic(source: Path, target: Path, mode: int = 0o644) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if source == target.resolve():
        return

    temporary = target.with_name(f".{target.name}.{secrets.token_hex(8)}.tmp")
    try:
        with source.open("rb") as source_handle, temporary.open("xb") as target_handle:
            shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _backup_existing(path: Path) -> Path | None:
    if not path.is_file():
        return None
    backup = path.with_name(path.name + ".bak")
    _copy_atomic(path.resolve(), backup, mode=path.stat().st_mode & 0o777)
    return backup


def _runtime_identity() -> tuple[int, int] | None:
    if not hasattr(os, "getuid") or not hasattr(os, "getgid"):
        return None
    uid = int(os.getenv("SUDO_UID", str(os.getuid())))
    gid = int(os.getenv("SUDO_GID", str(os.getgid())))
    # Running the Configurator directly as root must not silently make every
    # media container run as root. The documented deployment account defaults
    # to 1000 when sudo did not preserve an invoking identity.
    if uid == 0:
        uid = int(os.getenv("AI_CCTV_RUNTIME_UID", "1000"))
    if gid == 0:
        gid = int(os.getenv("AI_CCTV_RUNTIME_GID", "1000"))
    return uid, gid


def _validate_request(request: InstallRequest) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{3,64}", request.admin_username):
        raise ValueError("administrator username contains unsupported characters")
    if len(request.admin_password) < 12:
        raise ValueError("administrator password must contain at least 12 characters")
    if len(request.cameras) > 4:
        raise ValueError("at most four bootstrap cameras are supported")
    ip_address(request.public_bind_address)
    ip_address(request.rtsp_bind_address)
    model_source = request.model_path.expanduser()
    validate_custom_model(model_source)
    return model_source.resolve()


def initialize(request: InstallRequest) -> InstallResult:
    """Validate once and atomically generate config, secrets and Compose env."""

    model_source = _validate_request(request)
    root = request.data_root.expanduser().resolve()
    directories = {
        name: root / name
        for name in (
            "config",
            "secrets",
            "database",
            "recordings",
            "recovered",
            "snapshots",
            "models",
            "logs",
            "certs",
        )
    }
    for directory in directories.values():
        directory.mkdir(parents=True, exist_ok=True)
    runtime_identity = _runtime_identity()
    if runtime_identity is not None and os.geteuid() == 0:
        uid, gid = runtime_identity
        owned_names = (
            "database",
            "recordings",
            "recovered",
            "snapshots",
            "models",
            "logs",
        )
        try:
            for name in owned_names:
                os.chown(directories[name], uid, gid)
        except OSError as exc:
            explicitly_selected = any(
                os.getenv(name)
                for name in (
                    "SUDO_UID",
                    "SUDO_GID",
                    "AI_CCTV_RUNTIME_UID",
                    "AI_CCTV_RUNTIME_GID",
                )
            )
            if explicitly_selected or exc.errno not in {errno.EINVAL, errno.EPERM}:
                raise
            # Some rootless or mapped filesystems reject arbitrary numeric IDs.
            # Fall back to their actual owner so bind mounts remain writable.
            runtime_identity = (os.getuid(), os.getgid())
            for name in owned_names:
                stat = directories[name].stat()
                if (stat.st_uid, stat.st_gid) != runtime_identity:
                    os.chown(directories[name], *runtime_identity)

    installed_model = directories["models"] / model_source.name
    _backup_existing(installed_model)
    _copy_atomic(model_source, installed_model)
    container_model_path = PurePosixPath("/models") / installed_model.name

    config = AppConfig(
        server={
            "public_http_port": request.public_http_port,
            "public_https_port": request.public_https_port,
            "rtsp_bind_address": request.rtsp_bind_address,
            "rtsp_port": request.rtsp_port,
            "timezone": request.timezone,
        },
        recording={"root": str(directories["recordings"])},
        inference={"model_path": str(container_model_path)},
        cameras=request.cameras,
    )
    config_path = directories["config"] / "config.yaml"
    _backup_existing(config_path)
    write_config_atomic(config, config_path)
    os.chmod(config_path, 0o640)
    if runtime_identity is not None and os.geteuid() == 0:
        os.chown(config_path, *runtime_identity)

    password_hash = PasswordHasher().hash(request.admin_password)
    credentials = {
        camera.camera_id: {
            "username": camera.camera_id,
            "password": secrets.token_urlsafe(32),
        }
        for camera in request.cameras
    }
    secret_values = {
        "JWT_SECRET": secrets.token_urlsafe(48),
        "INTERNAL_SERVICE_TOKEN": secrets.token_urlsafe(48),
        "INITIAL_ADMIN_USERNAME": request.admin_username,
        "INITIAL_ADMIN_PASSWORD_HASH": password_hash,
        "MEDIA_PUBLISH_CREDENTIALS_JSON": json.dumps(
            credentials, separators=(",", ":")
        ),
    }
    secrets_path = directories["secrets"] / "secrets.env"
    _backup_existing(secrets_path)
    secrets_payload = "".join(
        f"{key}={_dotenv(value)}\n" for key, value in secret_values.items()
    )
    _write_atomic(secrets_path, secrets_payload, 0o600)
    camera_credentials_path = directories["secrets"] / "camera-credentials.json"
    _backup_existing(camera_credentials_path)
    _write_atomic(
        camera_credentials_path,
        json.dumps(credentials, ensure_ascii=False, indent=2) + "\n",
        0o600,
    )

    compose_values = {
        "CONFIG_FILE": config_path,
        "SECRETS_FILE": secrets_path,
        "DATABASE_DIR": directories["database"],
        "RECORDINGS_DIR": directories["recordings"],
        "RECOVERED_DIR": directories["recovered"],
        "SNAPSHOTS_DIR": directories["snapshots"],
        "MODELS_DIR": directories["models"],
        "MODEL_FILE": installed_model.name,
        "LOGS_DIR": directories["logs"],
        "CERTS_DIR": directories["certs"],
        "PUBLIC_HTTP_PORT": request.public_http_port,
        "PUBLIC_HTTPS_PORT": request.public_https_port,
        "PUBLIC_BIND_ADDRESS": request.public_bind_address,
        "RTSP_BIND_ADDRESS": request.rtsp_bind_address,
        "RTSP_PORT": request.rtsp_port,
    }
    if runtime_identity is not None:
        compose_values["AI_CCTV_UID"] = runtime_identity[0]
        compose_values["AI_CCTV_GID"] = runtime_identity[1]
    compose_env_path = request.server_dir.resolve() / ".env"
    _backup_existing(compose_env_path)
    compose_payload = "".join(
        f"{key}={_dotenv(value)}\n" for key, value in compose_values.items()
    )
    _write_atomic(compose_env_path, compose_payload, 0o600)
    return InstallResult(
        config_path,
        secrets_path,
        compose_env_path,
        camera_credentials_path,
        credentials,
    )
