"""화면과 독립적인 신규 설치 검사 및 기존 설치 정보 복원."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, replace
from ipaddress import ip_address
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

import yaml

from ai_cctv_core.config import AppConfig
from server.setup.config_core import (
    InstallRequest,
    InstallResult,
    _dotenv,
    _validate_public_base_url,
    _validate_request,
    _validate_tls_files,
    initialize,
)
from server.setup.model_manager import validate_custom_model
from server.setup.validation import deployment_path, read_deployment_env

from .compose_adapter import Prerequisite, installation_prerequisites


@dataclass(frozen=True)
class Installation:
    """관리 화면에서 복원할 공개 정보. 비밀번호와 서비스 토큰은 포함하지 않는다."""

    data_root: Path
    env_file: Path
    config_file: Path
    public_url: str
    rtsp_host: str
    rtsp_port: int
    admin_username: str


_MARKER = ".installation-in-progress"
_RECOVERY_MESSAGE = (
    "기존 설치 파일 또는 중단된 설치 흔적이 있습니다. 다시 초기화하지 마세요. "
    "현재 저장 폴더를 백업하고 상태 진단으로 설정·인증 파일을 확인한 뒤 "
    "백업에서 복구하거나 새 빈 저장 폴더를 선택하세요."
)
_SECRET_KEYS = (
    "DATA_SECRETS_FILE",
    "EXTERNAL_SECRETS_FILE",
    "PREPROCESSING_SECRETS_FILE",
    "MEDIA_SECRETS_FILE",
    "ANALYSIS_SECRETS_FILE",
)


# DB·설정·녹화 또는 중단 표식이 있으면 새 저장소로 간주하지 않아 재초기화를 막는다.
def _has_installation_traces(data_root: Path) -> bool:
    if not data_root.exists():
        return False
    if not data_root.is_dir():
        return True
    # 설치 프로그램이 만든 빈 저장 폴더, 미리 준비한 models/certs는 허용한다.
    names = (
        _MARKER,
        "config",
        "secrets",
        "database",
        "recordings",
        "recovered",
        "snapshots",
        "logs",
        "config.yaml",
        "compose.env",
        ".env",
    )
    return any((data_root / name).exists() for name in names) or any(
        path.is_file()
        and (".db" in path.suffixes or path.suffix in {".sqlite", ".sqlite3"})
        for path in data_root.iterdir()
    )


def inspect_installation(data_root: Path, server_dir: Path) -> Installation | None:
    """저장된 설치만 읽는다. 불완전한 상태를 신규 설치로 오인하지 않는다."""

    root = data_root.expanduser().resolve()
    server = server_dir.expanduser().resolve()
    env_file = root / "config" / "compose.env"
    try:
        if (root / _MARKER).exists():
            raise ValueError(_RECOVERY_MESSAGE)
        if not env_file.is_file():
            if _has_installation_traces(root):
                raise ValueError(_RECOVERY_MESSAGE)
            return None
        values = read_deployment_env(env_file)
        required = ("CONFIG_FILE", "DATABASE_DIR", *_SECRET_KEYS)
        if not all(values.get(key) for key in required):
            raise ValueError(_RECOVERY_MESSAGE)
        config_file = deployment_path(server, values["CONFIG_FILE"])
        raw = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        # AppConfig의 개발용 기본값이 잘린 설정 파일을 완성된 설치로 만들지 않게 한다.
        if not isinstance(raw, dict) or not all(
            key in raw for key in ("schema_version", "server", "recording", "inference")
        ):
            raise ValueError(_RECOVERY_MESSAGE)
        config = AppConfig.model_validate(raw)
        database = deployment_path(server, values["DATABASE_DIR"])
        if not database.is_dir():
            raise ValueError(_RECOVERY_MESSAGE)
        secret_paths = [deployment_path(server, values[key]) for key in _SECRET_KEYS]
        if len(set(secret_paths)) != len(secret_paths):
            raise ValueError(_RECOVERY_MESSAGE)
        for secret_file in secret_paths:
            if not secret_file.is_file() or not read_deployment_env(secret_file):
                raise ValueError(_RECOVERY_MESSAGE)
        data_values = read_deployment_env(secret_paths[0])
        admin_username = data_values.get("INITIAL_ADMIN_USERNAME", "")
        if not re.fullmatch(r"[A-Za-z0-9_.@-]{3,64}", admin_username):
            raise ValueError(_RECOVERY_MESSAGE)
        public_url = _validate_public_base_url(values.get("PUBLIC_BASE_URL", ""))
        bind = values.get("PUBLIC_BIND_ADDRESS", "127.0.0.1")
        ip_address(bind)
        if not public_url:
            host = "localhost" if bind in {"0.0.0.0", "::"} else bind
            authority = f"[{host}]" if ":" in host else host
            port = config.server.public_https_port
            public_url = f"https://{authority}" + (f":{port}" if port != 443 else "")
        rtsp_host = config.server.rtsp_bind_address
        if rtsp_host in {"0.0.0.0", "::"}:
            rtsp_host = urlsplit(public_url).hostname or "localhost"
        return Installation(
            data_root=root,
            env_file=env_file,
            config_file=config_file,
            public_url=public_url,
            rtsp_host=rtsp_host,
            rtsp_port=config.server.rtsp_port,
            admin_username=admin_username,
        )
    except (OSError, UnicodeError, ValueError, yaml.YAMLError):
        # YAML/Pydantic 오류에는 입력값이 들어갈 수 있으므로 화면에는 원문을 전달하지 않는다.
        raise ValueError(_RECOVERY_MESSAGE) from None


def preflight(
    server_dir: Path,
    model_path: Path | None,
    certificate_path: Path | None,
    private_key_path: Path | None,
) -> list[Prerequisite]:
    """파일을 쓰지 않고 Docker와 사용자가 준비한 모델·TLS를 검사한다."""

    labels = {
        "Server package": (
            "서버 구성 파일",
            "서버 실행 구성을 찾았습니다.",
            "서버 폴더의 compose.yml이 없습니다. 설치된 서버 패키지를 확인하세요.",
        ),
        "Docker Desktop": (
            "Docker Desktop",
            "Docker를 사용할 수 있습니다.",
            "Docker Desktop을 설치·실행한 뒤 준비가 끝나면 다시 검사하세요.",
        ),
        "Docker Engine": (
            "Docker 실행 상태",
            "Docker 엔진이 실행 중입니다.",
            "Docker Desktop을 실행하고 엔진 시작이 끝난 뒤 다시 검사하세요.",
        ),
        "Docker Compose": (
            "Docker Compose",
            "Docker Compose를 사용할 수 있습니다.",
            "Docker Desktop의 Compose 기능을 사용할 수 없습니다. 설치 상태를 확인하세요.",
        ),
    }
    original = installation_prerequisites(server_dir)
    results = []
    for item in original:
        name, success, failure = labels.get(
            item.name, (item.name, "검사를 통과했습니다.", "설치 환경을 확인하세요.")
        )
        results.append(Prerequisite(item.ok, name, success if item.ok else failure))
    if any(item.name == "Docker Engine" and item.ok for item in original):
        linux = False
        try:
            docker = shutil.which("docker")
            if docker:
                result = subprocess.run(
                    [docker, "info", "--format", "{{.OSType}}"],
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=20,
                )
                linux = (
                    result.returncode == 0 and result.stdout.strip().lower() == "linux"
                )
        except (OSError, subprocess.SubprocessError):
            pass
        results.append(
            Prerequisite(
                linux,
                "Linux 컨테이너 모드",
                "Linux 컨테이너 모드입니다."
                if linux
                else "Docker Desktop을 Linux 컨테이너 모드로 전환하고 다시 검사하세요.",
            )
        )

    model_ok = False
    model_message = "추론 모델 파일(.pt, .onnx, .engine)을 선택하세요."
    if model_path is not None:
        try:
            validate_custom_model(model_path.expanduser())
            model_ok = True
            model_message = (
                "모델 파일의 형식·크기를 확인했습니다. "
                "실제 추론 호환성은 서버 실행 후 확인해야 합니다."
            )
        except (OSError, ValueError):
            model_message = (
                "모델 파일을 읽을 수 없거나 형식·크기가 올바르지 않습니다. "
                "비어 있지 않은 2 GiB 이하의 .pt, .onnx, .engine 파일을 선택하세요."
            )
    results.append(Prerequisite(model_ok, "추론 모델", model_message))

    tls_ok = False
    tls_message = "HTTPS 인증서와 암호화되지 않은 개인키 파일을 모두 선택하세요."
    if certificate_path is not None and private_key_path is not None:
        try:
            _validate_tls_files(certificate_path, private_key_path)
            tls_ok = True
            tls_message = (
                "PEM 인증서와 개인키의 일치를 확인했습니다. "
                "접속 주소·유효기간·기기 신뢰 여부는 별도로 확인하세요."
            )
        except (OSError, ValueError):
            tls_message = (
                "인증서·개인키 파일을 읽을 수 없거나 서로 일치하지 않습니다. "
                "각 10 MiB 이하의 PEM 인증서와 암호화되지 않은 PEM 개인키를 선택하세요."
            )
    results.append(Prerequisite(tls_ok, "HTTPS 인증서와 개인키", tls_message))
    return results


# 모든 입력과 스키마를 쓰기 전에 확인하고 민감한 입력이 담길 수 있는 원래 오류는 가린다.
def _validate_install_input(request: InstallRequest) -> None:
    try:
        if request.tls_certificate_path is None or request.tls_private_key_path is None:
            raise ValueError("TLS files are required for installation")
        model = _validate_request(request)
        _validate_public_base_url(request.public_base_url)
        # Compose env를 마지막에 쓸 때까지 잘못된 경로 문자열 검사를 미루지 않는다.
        for value in (request.data_root, model.name, request.public_base_url):
            _dotenv(value)
        # initialize에서는 복사 뒤 실행되는 스키마 검사를 미리 수행한다.
        AppConfig(
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
                "model_path": str(PurePosixPath("/models") / model.name),
                "device": request.inference_device,
            },
            cameras=request.cameras,
        )
    except (OSError, ValueError, TypeError):
        raise ValueError(
            "설치 입력값을 확인하세요. 관리자 ID는 영문·숫자·_.@- 3~64자, "
            "비밀번호는 12자 이상이어야 합니다. 서로 다른 유효 포트, IP 주소, "
            "HTTPS 접속 주소, 중복 없는 카메라 설정과 모델·인증서 파일을 확인하세요."
        ) from None


def install_new(request: InstallRequest) -> InstallResult:
    """신규 저장소만 초기화한다. 실패한 여러 파일의 자동 롤백·재초기화는 하지 않는다."""

    root = request.data_root.expanduser().resolve()
    env_file = (
        request.compose_env_path.expanduser().resolve()
        if request.compose_env_path is not None
        else root / "config" / "compose.env"
    )
    request = replace(
        request,
        data_root=root,
        server_dir=request.server_dir.expanduser().resolve(),
        compose_env_path=env_file,
    )
    if _has_installation_traces(root) or env_file.exists():
        raise ValueError(_RECOVERY_MESSAGE)
    prerequisites = preflight(
        request.server_dir,
        request.model_path,
        request.tls_certificate_path,
        request.tls_private_key_path,
    )
    failures = [item.message for item in prerequisites if not item.ok]
    if failures:
        raise ValueError("\n".join(failures))
    _validate_install_input(request)
    # 오래 걸리는 사전 검사 사이에 다른 설치가 저장한 경우도 보호한다.
    if _has_installation_traces(root) or env_file.exists():
        raise ValueError(_RECOVERY_MESSAGE)
    marker = root / _MARKER
    try:
        root.mkdir(parents=True, exist_ok=True)
        # 배타 생성한 표식을 먼저 디스크에 남겨 중단·중복 실행을 완료된 설치로 오인하지 않게 한다.
        with marker.open("x", encoding="utf-8") as handle:
            handle.write("Installation started; preserve this folder if interrupted.\n")
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        raise ValueError(_RECOVERY_MESSAGE) from None
    except OSError:
        raise ValueError(
            "저장 폴더에 쓸 수 없습니다. 폴더 권한과 저장 공간을 확인하세요."
        ) from None
    try:
        result = initialize(request)
        marker.unlink()
        return result
    except Exception:
        # 원래 예외에는 비밀 설정값이 포함될 수 있다. 표식은 복구 확인 전까지 유지한다.
        raise RuntimeError(
            "설치 파일 생성 중 오류가 발생했습니다. " + _RECOVERY_MESSAGE
        ) from None
