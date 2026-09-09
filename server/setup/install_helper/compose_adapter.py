# GUI와 CLI가 동일한 Docker Compose 명령을 만들도록 실행 방법을 모은 어댑터다.
# 배포 env의 위치를 유지해야 새 프로젝트나 잘못된 저장소로 연결되는 일을 막을 수 있다.

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from server.setup.validation import read_deployment_env

START_ARGUMENTS = ("up", "-d", "--build", "--wait", "--remove-orphans")


# 소스 실행은 저장소의 server, 배포 실행 파일은 옆에 설치된 server 폴더를 기준으로 삼는다.
def default_server_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "server"
    return Path(__file__).resolve().parents[2]


# 명시한 저장소를 우선하고 기본값은 실행 코드와 분리된 OS별 영구 데이터 위치로 정한다.
def default_data_root() -> Path:
    configured = os.getenv("AI_CCTV_DATA_ROOT")
    if configured:
        return Path(configured).expanduser()
    program_data = os.getenv("PROGRAMDATA")
    if program_data:
        return Path(program_data) / "AI_CCTV"
    return Path.home() / ".local" / "share" / "AI_CCTV"


# 소스 실행과 설치된 실행 파일의 설정 위치가 다르므로 명시한 경로를 우선 선택한다.
def default_compose_env(server_dir: str | Path | None = None) -> Path:
    configured = os.getenv("AI_CCTV_COMPOSE_ENV_FILE")
    if configured:
        return Path(configured).expanduser().resolve()
    server_root = Path(server_dir or default_server_dir()).resolve()
    development_env = server_root / ".env"
    installed_env = default_data_root() / "config" / "compose.env"
    if getattr(sys, "frozen", False) or installed_env.is_file():
        return installed_env.resolve()
    return development_env


@dataclass(frozen=True)
class Prerequisite:
    ok: bool
    name: str
    message: str


def installation_prerequisites(server_dir: str | Path) -> list[Prerequisite]:
    """배포를 바꾸지 않고 실행 전 점검 항목을 반환한다."""

    server_root = Path(server_dir).resolve()
    results = []
    compose_file = server_root / "compose.yml"
    results.append(
        Prerequisite(
            compose_file.is_file(),
            "Server package",
            str(compose_file)
            if compose_file.is_file()
            else f"compose definition is missing: {compose_file}",
        )
    )
    docker = shutil.which("docker")
    if docker is None:
        results.append(
            Prerequisite(
                False,
                "Docker Desktop",
                "Docker was not found on PATH; install and start Docker Desktop.",
            )
        )
        return results
    try:
        info = subprocess.run(
            [docker, "info"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
        compose = subprocess.run(
            [docker, "compose", "version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        results.append(Prerequisite(False, "Docker Desktop", str(exc)))
        return results
    results.append(
        Prerequisite(
            info.returncode == 0,
            "Docker Engine",
            "running"
            if info.returncode == 0
            else "Docker is installed but its engine is not running.",
        )
    )
    results.append(
        Prerequisite(
            compose.returncode == 0,
            "Docker Compose",
            (compose.stdout or compose.stderr).strip()
            or "Docker Compose is unavailable.",
        )
    )
    return results


# 선택적 push 설정을 읽을 때 파일이 없으면 빈 구성으로 처리한다.
def _env_values(path: Path) -> dict[str, str]:
    return read_deployment_env(path)


# 한 번 선택한 Compose 정의와 env 경로를 모든 서비스 명령에 공통으로 사용한다.
class ComposeAdapter:
    def __init__(
        self, server_dir: str | Path, env_file: str | Path | None = None
    ) -> None:
        self.server_dir = Path(server_dir).resolve()
        self.compose_file = self.server_dir / "compose.yml"
        self.env_file = (
            Path(env_file).expanduser().resolve()
            if env_file is not None
            else default_compose_env(self.server_dir)
        )

    # FCM이 켜져 있으면 기본 env와 푸시 env를 함께 넣어 시작·중지·조회가 같은 구성을 사용하게 한다.
    def command(self, *arguments: str) -> list[str]:
        command = [
            "docker",
            "compose",
            "--env-file",
            str(self.env_file),
            "-f",
            str(self.compose_file),
        ]
        push_env = self.env_file.with_name("push.env")
        push_values = _env_values(push_env)
        if push_values.get("PUSH_ENABLED", "false").lower() in {"true", "1", "yes"}:
            command += [
                "--env-file",
                str(push_env),
                "-f",
                str(self.server_dir / "compose.push.yml"),
            ]
        return [*command, *arguments]

    # 서비스를 띄우기 전에 설정·모델·인증서가 실제 파일인지 확인한다. 실행 성공까지 보장하는 검사는 아니다.
    def deployment_prerequisites(self) -> list[Prerequisite]:
        results = installation_prerequisites(self.server_dir)
        if not self.env_file.is_file():
            results.append(
                Prerequisite(
                    False,
                    "Compose environment",
                    f"not initialized: {self.env_file}",
                )
            )
            return results
        results.append(Prerequisite(True, "Compose environment", str(self.env_file)))
        values = _env_values(self.env_file)
        push_values = _env_values(self.env_file.with_name("push.env"))
        if push_values.get("PUSH_ENABLED", "false").lower() in {"true", "1", "yes"}:
            values.update(push_values)

        # env의 상대 경로를 Compose 프로젝트 기준으로 해석하고 미설정 항목은 누락으로 표시한다.
        def deployment_path(key: str) -> Path | None:
            value = values.get(key)
            if not value:
                return None
            path = Path(value)
            return path if path.is_absolute() else (self.server_dir / path).resolve()

        required_files = {
            "Configuration": deployment_path("CONFIG_FILE"),
            "Data service secrets": deployment_path("DATA_SECRETS_FILE"),
            "External service secrets": deployment_path("EXTERNAL_SECRETS_FILE"),
            "Preprocessing service secrets": deployment_path(
                "PREPROCESSING_SECRETS_FILE"
            ),
            "Media service secrets": deployment_path("MEDIA_SECRETS_FILE"),
            "Analysis service secrets": deployment_path("ANALYSIS_SECRETS_FILE"),
        }
        if values.get("PUSH_ENABLED", "false").lower() in {"true", "1", "yes"}:
            required_files["Push Compose definition"] = (
                self.server_dir / "compose.push.yml"
            )
            required_files["Firebase service account"] = deployment_path(
                "FIREBASE_SERVICE_ACCOUNT_FILE"
            )
            results.append(
                Prerequisite(
                    bool(values.get("FIREBASE_PROJECT_ID")),
                    "Firebase project",
                    "Configured"
                    if values.get("FIREBASE_PROJECT_ID")
                    else "FIREBASE_PROJECT_ID is missing",
                )
            )
        models_root = deployment_path("MODELS_DIR")
        model_name = values.get("MODEL_FILE")
        required_files["Inference model"] = (
            models_root / model_name if models_root is not None and model_name else None
        )
        from server.setup.model_manager import (
            IDENTITY_PLUGIN,
            deployed_identity_model,
            validate_identity_model,
        )

        if (values.get("IDENTITY_PLUGIN") or IDENTITY_PLUGIN) == IDENTITY_PLUGIN:
            required_files["OSNet identity model"] = deployed_identity_model(
                values, models_root
            )
        certificate_root = deployment_path("CERTS_DIR")
        required_files["TLS certificate"] = (
            certificate_root / "tls.crt" if certificate_root is not None else None
        )
        required_files["TLS private key"] = (
            certificate_root / "tls.key" if certificate_root is not None else None
        )
        for name, path in required_files.items():
            present = path is not None and path.is_file()
            if present and name == "OSNet identity model":
                try:
                    validate_identity_model(path)
                except (OSError, ValueError):
                    present = False
            results.append(
                Prerequisite(
                    present,
                    name,
                    str(path)
                    if present
                    else f"missing or not configured: {path or name}",
                )
            )
        return results

    # 서버 폴더에서 명령을 실행하고 종료 코드를 호출자에게 넘겨 진단·CLI가 실패를 처리하게 한다.
    def run(
        self, *arguments: str, capture: bool = False
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            self.command(*arguments),
            cwd=self.server_dir,
            check=False,
            text=True,
            capture_output=capture,
        )

    def start(self) -> int:
        # 이전 구성의 컨테이너가 남아 중복 이벤트를 만들지 않도록 제거한다.
        return self.run(*START_ARGUMENTS).returncode

    def stop(self) -> int:
        return self.run("down").returncode

    def restart(self) -> int:
        result = self.run("restart")
        return result.returncode
