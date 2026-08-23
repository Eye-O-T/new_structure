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
from urllib.parse import urlsplit

from argon2 import PasswordHasher

from ai_cctv_core.config import AppConfig, CameraBootstrap, write_config_atomic

from .model_manager import validate_custom_model
from .private_files import restrict_private_file

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
    public_base_url: str = ""
    rtsp_bind_address: str = "127.0.0.1"
    rtsp_port: int = 8554
    timezone: str = "Asia/Seoul"


@dataclass(frozen=True)
class InstallResult:
    config_path: Path
    # Compatibility name: this is the Data service's least-privilege env file.
    secrets_path: Path
    external_secrets_path: Path
    inference_secrets_path: Path
    media_secrets_path: Path
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
        if mode & 0o077:
            os.chmod(temporary, mode)
        else:
            restrict_private_file(temporary)
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
        if mode & 0o077:
            os.chmod(temporary, mode)
        else:
            restrict_private_file(temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _backup_existing(path: Path, *, private: bool = False) -> Path | None:
    if not path.is_file():
        return None
    backup = path.with_name(path.name + ".bak")
    mode = 0o600 if private else path.stat().st_mode & 0o777
    _copy_atomic(path.resolve(), backup, mode=mode)
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


def _validate_public_base_url(value: str) -> str:
    """Return a normalized public HTTPS origin or an empty development value."""

    text = value.strip()
    if not text:
        return ""
    parsed = urlsplit(text)
    if parsed.scheme.lower() != "https":
        raise ValueError("public base URL must use HTTPS")
    if not parsed.hostname:
        raise ValueError("public base URL must include a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("public base URL must not contain credentials")
    if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
        raise ValueError(
            "public base URL must be an origin without path, query, or fragment"
        )
    try:
        parsed.port
    except ValueError as exc:
        raise ValueError("public base URL contains an invalid port") from exc
    return f"https://{parsed.netloc}"


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
    public_base_url = _validate_public_base_url(request.public_base_url)
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
        recording={
            "root": "/recordings",
            "recovery_root": "/recordings/recovered",
        },
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
    data_external_token = secrets.token_urlsafe(48)
    data_inference_token = secrets.token_urlsafe(48)
    data_media_token = secrets.token_urlsafe(48)
    data_recovery_token = secrets.token_urlsafe(48)
    media_read_username = "inference-reader"
    media_read_password = secrets.token_urlsafe(48)
    data_secret_values = {
        "DATA_EXTERNAL_TOKEN": data_external_token,
        "DATA_INFERENCE_TOKEN": data_inference_token,
        "DATA_MEDIA_TOKEN": data_media_token,
        "DATA_RECOVERY_TOKEN": data_recovery_token,
        "INITIAL_ADMIN_USERNAME": request.admin_username,
        "INITIAL_ADMIN_PASSWORD_HASH": password_hash,
    }
    external_secret_values = {
        "DATA_EXTERNAL_TOKEN": data_external_token,
        "JWT_SECRET": secrets.token_urlsafe(48),
        "MEDIA_READ_USERNAME": media_read_username,
        "MEDIA_READ_PASSWORD": media_read_password,
        "MEDIA_PUBLISH_CREDENTIALS_JSON": json.dumps(
            credentials, separators=(",", ":")
        ),
    }
    inference_secret_values = {
        "DATA_INFERENCE_TOKEN": data_inference_token,
        "MEDIA_READ_USERNAME": media_read_username,
        "MEDIA_READ_PASSWORD": media_read_password,
    }
    media_secret_values = {
        "DATA_MEDIA_TOKEN": data_media_token,
    }
    secret_files = {
        directories["secrets"] / "data.env": data_secret_values,
        directories["secrets"] / "external.env": external_secret_values,
        directories["secrets"] / "inference.env": inference_secret_values,
        directories["secrets"] / "media.env": media_secret_values,
    }
    for path, values in secret_files.items():
        _backup_existing(path, private=True)
        payload = "".join(f"{key}={_dotenv(value)}\n" for key, value in values.items())
        _write_atomic(path, payload, 0o600)
    (
        secrets_path,
        external_secrets_path,
        inference_secrets_path,
        media_secrets_path,
    ) = secret_files
    camera_credentials_path = directories["secrets"] / "camera-credentials.json"
    _backup_existing(camera_credentials_path, private=True)
    _write_atomic(
        camera_credentials_path,
        json.dumps(credentials, ensure_ascii=False, indent=2) + "\n",
        0o600,
    )

    compose_values = {
        "CONFIG_FILE": config_path,
        "DATA_SECRETS_FILE": secrets_path,
        "EXTERNAL_SECRETS_FILE": external_secrets_path,
        "INFERENCE_SECRETS_FILE": inference_secrets_path,
        "MEDIA_SECRETS_FILE": media_secrets_path,
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
        "PUBLIC_BASE_URL": public_base_url,
        "RTSP_BIND_ADDRESS": request.rtsp_bind_address,
        "RTSP_PORT": request.rtsp_port,
    }
    if runtime_identity is not None:
        compose_values["AI_CCTV_UID"] = runtime_identity[0]
        compose_values["AI_CCTV_GID"] = runtime_identity[1]
    compose_env_path = request.server_dir.resolve() / ".env"
    _backup_existing(compose_env_path, private=True)
    compose_payload = "".join(
        f"{key}={_dotenv(value)}\n" for key, value in compose_values.items()
    )
    _write_atomic(compose_env_path, compose_payload, 0o600)
    return InstallResult(
        config_path=config_path,
        secrets_path=secrets_path,
        external_secrets_path=external_secrets_path,
        inference_secrets_path=inference_secrets_path,
        media_secrets_path=media_secrets_path,
        compose_env_path=compose_env_path,
        camera_credentials_path=camera_credentials_path,
        camera_credentials=credentials,
    )
