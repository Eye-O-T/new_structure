# 설치 화면·CLI의 입력을 실제 운영 설정, 모델, 인증 파일로 바꾸는 공통 로직이다.
# 호스트 경로와 컨테이너 내부 경로를 구분하고, 파일별 임시 저장 후 교체로 불완전한 파일을 막는다.

from __future__ import annotations

import errno
import json
import os
import re
import secrets
import shutil
import ssl
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from argon2 import PasswordHasher

from ai_cctv_core.config import AppConfig, CameraBootstrap, write_config_atomic

from .model_manager import (
    IDENTITY_PLUGIN,
    MAX_IDENTITY_MODEL_BYTES,
    install_local_model,
    resolve_identity_model,
    sha256_file,
    validate_custom_model,
)
from .private_files import restrict_private_file
from .validation import read_deployment_env

SAFE_ENV = re.compile(r"^[A-Za-z0-9_./:@+-]+$")
AI_CCTV_VERSION = "0.3.0"
RELEASE_IMAGES = {
    "data": f"ai-cctv-data:{AI_CCTV_VERSION}",
    "external": f"ai-cctv-external:{AI_CCTV_VERSION}",
    "preprocessing": f"ai-cctv-preprocessing:{AI_CCTV_VERSION}",
    "analysis": f"ai-cctv-analysis:{AI_CCTV_VERSION}",
    "mediamtx": "ai-cctv-mediamtx:1.9.0",
    "mediamtx_upstream": "bluenviron/mediamtx:1.9.0",
    "nginx": "nginx:1.27.2-alpine",
}


# GUI와 CLI가 공통 초기화에 넘기는 입력 묶음이며 경로는 호스트 파일을 가리킨다.
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
    recording_segment_seconds: int = 60
    retention_days: int = 7
    storage_warning_free_percent: int = 15
    inference_device: str = "auto"
    timezone: str = "Asia/Seoul"
    compose_env_path: Path | None = None
    tls_certificate_path: Path | None = None
    tls_private_key_path: Path | None = None
    identity_model_path: Path | None = None


# 생성된 파일 경로와 초기 카메라 인계 정보를 반환한다. 비밀값은 화면에 출력하지 않는다.
@dataclass(frozen=True)
class InstallResult:
    config_path: Path
    # 기존 필드명이며 실제로는 Data 전용 비밀 설정 파일이다.
    secrets_path: Path
    external_secrets_path: Path
    preprocessing_secrets_path: Path
    analysis_secrets_path: Path
    media_secrets_path: Path
    compose_env_path: Path
    camera_credentials_path: Path
    release_manifest_path: Path
    tls_certificate_path: Path
    tls_private_key_path: Path
    camera_credentials: dict[str, dict[str, str]]


# 작은따옴표로 감싸 비밀번호 해시의 $가 Compose 변수로 치환되는 것을 막는다.
def _dotenv(value: str | Path | int) -> str:
    text = str(value)
    if "\n" in text or "\r" in text:
        raise ValueError("environment values must be single-line")
    if SAFE_ENV.fullmatch(text):
        return text
    return "'" + text.replace("'", "\\'") + "'"


# 임시 파일에 쓰기와 디스크 반영을 마친 뒤 이름을 교체한다. 여러 파일 전체의 트랜잭션은 아니다.
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


# 같은 파일의 복사는 생략하고 임시 복사본에 권한을 적용한 뒤 완성된 파일로 교체한다.
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


# 덮어쓰기 전 .bak 사본을 남기며 비밀 파일의 백업에도 제한된 접근 권한을 적용한다.
def _backup_existing(path: Path, *, private: bool = False) -> Path | None:
    if not path.is_file():
        return None
    backup = path.with_name(path.name + ".bak")
    mode = 0o600 if private else path.stat().st_mode & 0o777
    _copy_atomic(path.resolve(), backup, mode=mode)
    return backup


# sudo 호출자의 계정 또는 지정된 런타임 계정으로 Linux 컨테이너의 UID·GID를 결정한다.
def _runtime_identity() -> tuple[int, int] | None:
    if not hasattr(os, "getuid") or not hasattr(os, "getgid"):
        return None
    uid = int(os.getenv("SUDO_UID", str(os.getuid())))
    gid = int(os.getenv("SUDO_GID", str(os.getgid())))
    # root로 설치해도 미디어 컨테이너는 일반 계정으로 실행한다. 기본 UID·GID는 1000이다.
    if uid == 0:
        uid = int(os.getenv("AI_CCTV_RUNTIME_UID", "1000"))
    if gid == 0:
        gid = int(os.getenv("AI_CCTV_RUNTIME_GID", "1000"))
    return uid, gid


def _validate_public_base_url(value: str) -> str:
    """HTTPS 접속 주소를 검증한다. 개발 환경에서는 빈 값을 허용한다."""

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


# TLS 라이브러리로 인증서와 키를 함께 로드해 PEM 헤더 검사만으로 놓치는 불일치를 잡는다.
def _validate_certificate_key_match(certificate: Path, private_key: Path) -> None:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        context.load_cert_chain(certfile=certificate, keyfile=private_key)
    except (OSError, ssl.SSLError) as exc:
        raise ValueError(
            "TLS certificate/private key are invalid or do not match"
        ) from exc


# 인증서와 개인키를 읽을 수 있는지, 서로 한 쌍인지 확인한 뒤에만 운영 위치에 복사한다.
def _validate_tls_files(certificate: Path, private_key: Path) -> tuple[Path, Path]:
    certificate = certificate.expanduser()
    private_key = private_key.expanduser()
    for path, label in (
        (certificate, "TLS certificate"),
        (private_key, "TLS private key"),
    ):
        if not path.is_file():
            raise ValueError(f"{label} does not exist or is not a file: {path}")
        if path.stat().st_size == 0:
            raise ValueError(f"{label} is empty")
        if path.stat().st_size > 10 * 1024 * 1024:
            raise ValueError(f"{label} exceeds the 10 MiB size limit")
    certificate = certificate.resolve()
    private_key = private_key.resolve()
    if certificate == private_key:
        raise ValueError("TLS certificate and private key must be different files")
    certificate_header = certificate.read_bytes()[:8192]
    private_key_header = private_key.read_bytes()[:8192]
    if b"-----BEGIN CERTIFICATE-----" not in certificate_header:
        raise ValueError("TLS certificate is not a PEM certificate")
    if b"ENCRYPTED" in private_key_header or not (
        b"-----BEGIN PRIVATE KEY-----" in private_key_header
        or b"-----BEGIN RSA PRIVATE KEY-----" in private_key_header
        or b"-----BEGIN EC PRIVATE KEY-----" in private_key_header
    ):
        raise ValueError(
            "TLS private key must be an unencrypted PEM private key supported by Nginx"
        )
    _validate_certificate_key_match(certificate, private_key)
    return certificate, private_key


# 인증서와 개인키는 함께 생략하거나 함께 지정해야 하며 한쪽만 바꾸는 요청은 거부한다.
def _validate_tls_pair(request: InstallRequest) -> tuple[Path, Path] | None:
    certificate = request.tls_certificate_path
    private_key = request.tls_private_key_path
    if (certificate is None) != (private_key is None):
        raise ValueError("TLS certificate and private key must be provided together")
    if certificate is None or private_key is None:
        return None
    return _validate_tls_files(certificate, private_key)


# 관리자·포트·녹화·추론 설정과 파일을 검증하고 파일 생성 전에 사용할 모델 경로를 확정한다.
def _validate_request(request: InstallRequest) -> Path:
    if not re.fullmatch(r"[A-Za-z0-9_.@-]{3,64}", request.admin_username):
        raise ValueError("administrator username contains unsupported characters")
    if len(request.admin_password) < 12:
        raise ValueError("administrator password must contain at least 12 characters")
    if len(request.cameras) > 4:
        raise ValueError("at most four bootstrap cameras are supported")
    ports = (
        request.public_http_port,
        request.public_https_port,
        request.rtsp_port,
    )
    if any(isinstance(port, bool) or not 1 <= port <= 65_535 for port in ports):
        raise ValueError("HTTP, HTTPS and RTSP ports must be in range 1..65535")
    if len(set(ports)) != len(ports):
        raise ValueError("HTTP, HTTPS and RTSP ports must be distinct")
    if not 10 <= request.recording_segment_seconds <= 300:
        raise ValueError("recording segment seconds must be in range 10..300")
    if request.retention_days < 1:
        raise ValueError("recording retention days must be at least 1")
    if not 1 <= request.storage_warning_free_percent <= 99:
        raise ValueError("storage warning free percent must be in range 1..99")
    if re.fullmatch(r"(?:auto|cpu|cuda(?::[0-9]+)?)", request.inference_device) is None:
        raise ValueError(
            "inference device must be auto, cpu, cuda, or cuda:<non-negative index>"
        )
    ip_address(request.public_bind_address)
    ip_address(request.rtsp_bind_address)
    model_source = request.model_path.expanduser()
    validate_custom_model(model_source)
    _validate_tls_pair(request)
    identity_source = resolve_identity_model(
        request.identity_model_path, request.data_root, request.server_dir
    )
    # 두 모델은 같은 디렉터리에 복사하므로 Windows에서도 서로 다른 대상이어야 한다.
    if identity_source.name.casefold() == model_source.name.casefold():
        raise ValueError(
            "detection and identity models must use different filenames "
            "(case-insensitive)"
        )
    return model_source.resolve()


# 입력 검증 → 운영 폴더·모델 준비 → 설정·인증 파일 생성 순서다. 기존 파일은 백업한다.
def initialize(request: InstallRequest) -> InstallResult:
    model_source = _validate_request(request)
    identity_source = resolve_identity_model(
        request.identity_model_path, request.data_root, request.server_dir
    )
    public_base_url = _validate_public_base_url(request.public_base_url)
    root = request.data_root.expanduser().resolve()
    compose_env_path = (
        request.compose_env_path.expanduser().resolve()
        if request.compose_env_path is not None
        else request.server_dir.resolve() / ".env"
    )
    previous_environment = read_deployment_env(compose_env_path)
    identity_companions = []
    for suffix in (".onnx.json", ".LICENSE.txt"):
        companion = identity_source.with_suffix(suffix)
        if companion.exists():
            if (
                not companion.is_file()
                or not 0 < companion.stat().st_size <= 1024 * 1024
            ):
                raise ValueError(
                    "OSNet provenance/license must be a non-empty file up to 1 MiB"
                )
            identity_companions.append((companion, suffix))
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
            # 소유자 변경이 불가능한 파일 시스템에서는 현재 소유자로 컨테이너를 실행한다.
            runtime_identity = (os.getuid(), os.getgid())
            for name in owned_names:
                stat = directories[name].stat()
                if (stat.st_uid, stat.st_gid) != runtime_identity:
                    os.chown(directories[name], *runtime_identity)

    certificate_path = directories["certs"] / "tls.crt"
    private_key_path = directories["certs"] / "tls.key"
    tls_pair = _validate_tls_pair(request)
    if tls_pair is None and (certificate_path.exists() or private_key_path.exists()):
        if not (certificate_path.is_file() and private_key_path.is_file()):
            raise ValueError(
                "persistent TLS certificate/private key are incomplete; select both"
            )
        _validate_tls_files(certificate_path, private_key_path)
    if tls_pair is not None:
        certificate_source, private_key_source = tls_pair
        for source, target, private in (
            (certificate_source, certificate_path, False),
            (private_key_source, private_key_path, True),
        ):
            expected_digest = sha256_file(source)
            _backup_existing(target, private=private)
            _copy_atomic(source, target, mode=0o600 if private else 0o644)
            if sha256_file(target) != expected_digest:
                raise OSError(f"installed TLS file verification failed: {target.name}")

    installed_model = directories["models"] / model_source.name
    _backup_existing(installed_model)
    installed_model = install_local_model(model_source, directories["models"])
    container_model_path = PurePosixPath("/models") / installed_model.name
    _backup_existing(directories["models"] / identity_source.name)
    installed_identity = install_local_model(
        identity_source, directories["models"], max_bytes=MAX_IDENTITY_MODEL_BYTES
    )
    container_identity_path = PurePosixPath("/models") / installed_identity.name
    # 배포 출처와 라이선스를 ONNX와 함께 전달하되 모델 실행에는 ONNX만 필요하다.
    for companion, suffix in identity_companions:
        target = installed_identity.with_suffix(suffix)
        _backup_existing(target)
        _copy_atomic(companion, target)
        if sha256_file(companion) != sha256_file(target):
            raise OSError("OSNet provenance/license copy verification failed")

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
            "segment_seconds": request.recording_segment_seconds,
            "retention_days": request.retention_days,
            "warning_free_percent": request.storage_warning_free_percent,
        },
        inference={
            "model_path": str(container_model_path),
            "device": request.inference_device,
        },
        cameras=request.cameras,
    )
    config_path = directories["config"] / "config.yaml"
    _backup_existing(config_path)
    write_config_atomic(config, config_path)
    os.chmod(config_path, 0o640)
    if runtime_identity is not None and os.geteuid() == 0:
        os.chown(config_path, *runtime_identity)

    release_manifest_path = directories["config"] / "release-manifest.json"
    _backup_existing(release_manifest_path)
    release_manifest = {
        "schema_version": 1,
        "application_version": AI_CCTV_VERSION,
        "python_version": "3.11.9",
        "images": RELEASE_IMAGES,
        "model": {
            "filename": installed_model.name,
            "sha256": sha256_file(installed_model),
        },
        "identity_model": {
            "plugin": IDENTITY_PLUGIN,
            "filename": installed_identity.name,
            "sha256": sha256_file(installed_identity),
        },
    }
    _write_atomic(
        release_manifest_path,
        json.dumps(release_manifest, ensure_ascii=False, indent=2) + "\n",
        0o640,
    )
    if runtime_identity is not None and os.geteuid() == 0:
        os.chown(release_manifest_path, *runtime_identity)

    # 관리자 암호는 Argon2 해시로 저장하고 서비스 호출·영상 읽기·카메라 게시 권한은 별도로 발급한다.
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
    data_identity_token = secrets.token_urlsafe(48)
    data_analysis_token = secrets.token_urlsafe(48)
    media_read_username = "inference-reader"
    media_read_password = secrets.token_urlsafe(48)
    data_secret_values = {
        "DATA_EXTERNAL_TOKEN": data_external_token,
        "DATA_INFERENCE_TOKEN": data_inference_token,
        "DATA_MEDIA_TOKEN": data_media_token,
        "DATA_RECOVERY_TOKEN": data_recovery_token,
        "DATA_IDENTITY_TOKEN": data_identity_token,
        "DATA_ANALYSIS_TOKEN": data_analysis_token,
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
    preprocessing_secret_values = {
        "DATA_IDENTITY_TOKEN": data_identity_token,
        "DATA_INFERENCE_TOKEN": data_inference_token,
        "MEDIA_READ_USERNAME": media_read_username,
        "MEDIA_READ_PASSWORD": media_read_password,
    }
    media_secret_values = {
        "DATA_MEDIA_TOKEN": data_media_token,
    }
    # Data는 역할별 토큰의 검증자이며 각 소비 서비스에는 자신에게 필요한 토큰만 배포한다.
    secret_files = {
        directories["secrets"] / "data.env": data_secret_values,
        directories["secrets"] / "external.env": external_secret_values,
        directories["secrets"] / "preprocessing.env": preprocessing_secret_values,
        directories["secrets"] / "media.env": media_secret_values,
        directories["secrets"] / "analysis.env": {
            "DATA_ANALYSIS_TOKEN": data_analysis_token
        },
    }
    for path, values in secret_files.items():
        _backup_existing(path, private=True)
        payload = "".join(f"{key}={_dotenv(value)}\n" for key, value in values.items())
        _write_atomic(path, payload, 0o600)
    secrets_path = directories["secrets"] / "data.env"
    external_secrets_path = directories["secrets"] / "external.env"
    preprocessing_secrets_path = directories["secrets"] / "preprocessing.env"
    analysis_secrets_path = directories["secrets"] / "analysis.env"
    media_secrets_path = directories["secrets"] / "media.env"
    camera_credentials_path = directories["secrets"] / "camera-credentials.json"
    _backup_existing(camera_credentials_path, private=True)
    _write_atomic(
        camera_credentials_path,
        json.dumps(credentials, ensure_ascii=False, indent=2) + "\n",
        0o600,
    )

    # Compose에는 호스트 마운트 경로를 기록하고 애플리케이션 YAML에는 컨테이너 내부 경로를 사용한다.
    compose_values = {
        "AI_CCTV_VERSION": AI_CCTV_VERSION,
        "CONFIG_FILE": config_path,
        "DATA_SECRETS_FILE": secrets_path,
        "EXTERNAL_SECRETS_FILE": external_secrets_path,
        "PREPROCESSING_SECRETS_FILE": preprocessing_secrets_path,
        "MEDIA_SECRETS_FILE": media_secrets_path,
        "ANALYSIS_SECRETS_FILE": directories["secrets"] / "analysis.env",
        "DATABASE_DIR": directories["database"],
        "RECORDINGS_DIR": directories["recordings"],
        "RECOVERED_DIR": directories["recovered"],
        "SNAPSHOTS_DIR": directories["snapshots"],
        "MODELS_DIR": directories["models"],
        "MODEL_FILE": installed_model.name,
        "IDENTITY_PLUGIN": IDENTITY_PLUGIN,
        "IDENTITY_MODEL_PATH": str(container_identity_path),
        "IDENTITY_MATCH_THRESHOLD": previous_environment.get(
            "IDENTITY_MATCH_THRESHOLD", "0.97"
        ),
        "IDENTITY_MATCH_MARGIN": previous_environment.get(
            "IDENTITY_MATCH_MARGIN", "0.05"
        ),
        "LOGS_DIR": directories["logs"],
        "CERTS_DIR": directories["certs"],
        "PUBLIC_HTTP_PORT": request.public_http_port,
        "PUBLIC_HTTPS_PORT": request.public_https_port,
        "PUBLIC_BIND_ADDRESS": request.public_bind_address,
        "PUBLIC_BASE_URL": public_base_url,
        "RTSP_BIND_ADDRESS": request.rtsp_bind_address,
        "RTSP_PORT": request.rtsp_port,
        "RECORDING_SEGMENT_SECONDS": request.recording_segment_seconds,
    }
    if runtime_identity is not None:
        compose_values["AI_CCTV_UID"] = runtime_identity[0]
        compose_values["AI_CCTV_GID"] = runtime_identity[1]
    _backup_existing(compose_env_path, private=True)
    compose_payload = "".join(
        f"{key}={_dotenv(value)}\n" for key, value in compose_values.items()
    )
    # 참조 대상 파일을 준비한 뒤 마지막으로 Compose 연결 정보를 저장한다.
    _write_atomic(compose_env_path, compose_payload, 0o600)
    return InstallResult(
        config_path=config_path,
        secrets_path=secrets_path,
        external_secrets_path=external_secrets_path,
        preprocessing_secrets_path=preprocessing_secrets_path,
        analysis_secrets_path=analysis_secrets_path,
        media_secrets_path=media_secrets_path,
        compose_env_path=compose_env_path,
        camera_credentials_path=camera_credentials_path,
        release_manifest_path=release_manifest_path,
        tls_certificate_path=certificate_path,
        tls_private_key_path=private_key_path,
        camera_credentials=credentials,
    )
