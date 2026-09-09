"""선택한 저장소와 실행 중인 배포를 대조한 뒤 제한 시간 안에 관리한다."""

from __future__ import annotations

import json
import os
import posixpath
import re
import signal
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from server.setup.validation import deployment_path, read_deployment_env

from .compose_adapter import START_ARGUMENTS, ComposeAdapter
from .workflow import Installation


_WINDOWS = os.name == "nt"
START_TIMEOUT_SECONDS = 30 * 60
ACTION_TIMEOUT_SECONDS = 2 * 60
QUERY_TIMEOUT_SECONDS = 30
_DOCKER_ERROR = (
    "Docker 작업을 확인하지 못했습니다. Docker Desktop의 실행 상태와 서버 파일을 "
    "확인한 뒤 다시 시도하세요. 설정과 인증키는 다시 생성하지 않았습니다."
)
_OTHER_DEPLOYMENT = (
    "현재 Docker 서버는 선택한 저장 위치와 다른 배포입니다. 기존 서버의 저장 위치를 "
    "선택해 중지한 뒤 이 저장 위치로 돌아오세요. 기존 서버는 변경하지 않았습니다."
)
_UNKNOWN_DEPLOYMENT = (
    "현재 컨테이너의 저장 위치를 확인할 수 없어 작업을 중단했습니다. "
    "기존 서버의 저장 위치와 Docker 상태를 확인하세요. 기존 서버는 변경하지 않았습니다."
)
_CONFIG_MISMATCH = (
    "실행 구성이 선택한 설정 파일·데이터베이스 위치와 다릅니다. "
    "저장된 설정과 환경 변수의 경로를 확인하세요. 서버는 변경하지 않았습니다."
)
_TIMEOUT_MESSAGE = (
    "작업 제한 시간을 넘겨 로컬 실행 프로세스를 중단했습니다. "
    "Docker에 일부 작업이 반영되었을 수 있습니다. Docker Desktop과 서버 상태를 "
    "확인한 뒤 다시 시도하세요. 설정과 인증키는 다시 생성하지 않았습니다."
)
_SERVICES = {
    "data": "데이터 저장",
    "external": "외부 API",
    "preprocessing": "영상 분석",
    "analysis": "이벤트 처리",
    "mediamtx": "영상 수신·녹화",
    "nginx": "HTTPS 접속",
    "push": "알림 전송",
}
_STATES = {
    "running": "실행 중",
    "exited": "중지됨",
    "created": "생성됨",
    "paused": "일시 중지",
    "restarting": "재시작 중",
    "removing": "제거 중",
    "dead": "실행 오류",
}
_HEALTH = {"healthy": "정상", "unhealthy": "점검 필요", "starting": "준비 중"}


def _host_path(value: str) -> str:
    """Docker Desktop의 Linux 표기를 Windows 호스트 경로와 비교한다."""

    path = value.replace("\\", "/")
    if path.startswith("//?/"):
        path = path[4:]
        if path.lower().startswith("unc/"):
            path = "//" + path[4:]
    for prefix in ("/run/desktop/mnt/host/", "/host_mnt/"):
        if path.lower().startswith(prefix):
            remainder = path[len(prefix) :]
            if re.match(r"^[A-Za-z](?:/|$)", remainder):
                path = remainder[0] + ":/" + remainder[2:]
            break
    path = posixpath.normpath(path)
    if re.match(r"^[A-Za-z]:/", path) or path.startswith("//"):
        return path.casefold()
    return path


def _terminate_process_tree(process: subprocess.Popen) -> None:
    """이번 작업이 시작한 프로세스 트리만 정리하고 Docker 컨테이너는 건드리지 않는다."""

    tree_stopped = False
    if _WINDOWS:
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            tree_stopped = result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            pass
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
            tree_stopped = True
        except OSError:
            pass
    if process.poll() is None:
        process.kill()
    process.communicate(timeout=5)
    if not tree_stopped:
        raise OSError("process tree termination could not be confirmed")


# 전체 작업의 남은 시간 안에 명령을 실행한다. 조회는 더 짧게 제한하고 원시 오류 출력은 버린다.
def _run(
    command: list[str], *, directory: Path, deadline: float, capture: bool = True
) -> str:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError(_TIMEOUT_MESSAGE)
    options: dict[str, Any] = {
        "cwd": directory,
        "stdout": subprocess.PIPE if capture else subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if _WINDOWS:
        options["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    else:
        options["start_new_session"] = True
    try:
        process = subprocess.Popen(command, **options)
    except (OSError, ValueError):
        raise RuntimeError(_DOCKER_ERROR) from None
    try:
        stdout, _ = process.communicate(
            timeout=min(remaining, QUERY_TIMEOUT_SECONDS) if capture else remaining
        )
    except subprocess.TimeoutExpired:
        try:
            _terminate_process_tree(process)
        except (OSError, subprocess.SubprocessError):
            raise RuntimeError(
                "제한 시간을 넘겼으며 실행 프로세스 종료를 확인하지 못했습니다. "
                "Docker Desktop의 작업 상태를 확인한 뒤 다시 시도하세요."
            ) from None
        raise RuntimeError(_TIMEOUT_MESSAGE) from None
    if process.returncode:
        raise RuntimeError(_DOCKER_ERROR)
    return stdout or ""


# Docker JSON 파싱 실패를 공통 오류로 바꿔 응답 본문이 화면으로 흘러가지 않게 한다.
def _json(text: str) -> Any:
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        raise RuntimeError(_DOCKER_ERROR) from None


# env 파싱 등 명령 구성 단계의 오류도 원시 값 대신 같은 운영 안내로 변환한다.
def _command(adapter: ComposeAdapter, *arguments: str) -> list[str]:
    try:
        return adapter.command(*arguments)
    except (OSError, UnicodeError, ValueError):
        raise RuntimeError(_DOCKER_ERROR) from None


# Docker가 반환한 값이 컨테이너 ID 형식인지 확인한 뒤 후속 inspect 인수로 사용한다.
def _ids(text: str) -> list[str]:
    values = text.split()
    if any(not re.fullmatch(r"[a-f0-9]{12,64}", value) for value in values):
        raise RuntimeError(_DOCKER_ERROR)
    return values


# 설정 JSON과 실제 컨테이너의 서로 다른 필드 이름을 맞춰 필수 bind 마운트를 대조한다.
def _mounts_match(mounts: Any, expected: dict[str, Path], *, resolved: bool) -> bool:
    if not isinstance(mounts, list):
        return False
    for target, source in expected.items():
        matches = [
            mount
            for mount in mounts
            if isinstance(mount, dict)
            and mount.get("Destination" if resolved else "target") == target
        ]
        if len(matches) != 1:
            return False
        mount = matches[0]
        actual = mount.get("Source" if resolved else "source")
        if (
            mount.get("Type" if resolved else "type") != "bind"
            or not isinstance(actual, str)
            or _host_path(actual) != _host_path(str(source))
        ):
            return False
    return True


# Compose 프로젝트명만 믿지 않고 Data의 설정·DB 마운트로 선택한 설치와의 일치를 확인한다.
def _deployment_state(
    adapter: ComposeAdapter, installed: Installation, deadline: float
) -> str:
    try:
        values = read_deployment_env(installed.env_file)
        config_file = deployment_path(adapter.server_dir, values["CONFIG_FILE"])
        database = deployment_path(adapter.server_dir, values["DATABASE_DIR"])
        if config_file != installed.config_file.resolve():
            raise ValueError("configuration changed")
    except (OSError, UnicodeError, ValueError, KeyError):
        raise RuntimeError(_CONFIG_MISMATCH) from None
    expected = {
        "/app/config/config.yaml": config_file,
        "/data/database": database,
    }
    configuration = _json(
        _run(
            _command(adapter, "config", "--format", "json"),
            directory=adapter.server_dir,
            deadline=deadline,
        )
    )
    if not isinstance(configuration, dict):
        raise RuntimeError(_DOCKER_ERROR)
    project = configuration.get("name")
    if not isinstance(project, str) or not re.fullmatch(
        r"[a-z0-9][a-z0-9_-]*", project
    ):
        raise RuntimeError(_DOCKER_ERROR)
    services = configuration.get("services")
    data = services.get("data") if isinstance(services, dict) else None
    if not isinstance(data, dict) or not _mounts_match(
        data.get("volumes"), expected, resolved=False
    ):
        raise RuntimeError(_CONFIG_MISMATCH)
    query = [
        "docker",
        "ps",
        "--all",
        "--filter",
        f"label=com.docker.compose.project={project}",
        "--format",
        "{{.ID}}",
    ]
    containers = _ids(_run(query, directory=adapter.server_dir, deadline=deadline))
    if not containers:
        return "empty"
    # Data가 없으면 저장소 소유 관계를 증명할 수 없으므로 남은 컨테이너를 임의로 변경하지 않는다.
    data_containers = _ids(
        _run(
            [*query, "--filter", "label=com.docker.compose.service=data"],
            directory=adapter.server_dir,
            deadline=deadline,
        )
    )
    if not data_containers or not set(data_containers).issubset(containers):
        return "unknown"
    mounts_output = _run(
        ["docker", "inspect", "--format", "{{json .Mounts}}", *data_containers],
        directory=adapter.server_dir,
        deadline=deadline,
    )
    lines = [line for line in mounts_output.splitlines() if line.strip()]
    if len(lines) != len(data_containers):
        return "unknown"
    if not all(_mounts_match(_json(line), expected, resolved=True) for line in lines):
        return "different"
    return "matching"


# 알려진 서비스·상태 이름만 표시해 Docker 응답에 포함될 수 있는 임의 값 노출을 제한한다.
def _status(adapter: ComposeAdapter, deadline: float) -> str:
    output = _run(
        _command(adapter, "ps", "--all", "--format", "json"),
        directory=adapter.server_dir,
        deadline=deadline,
    ).strip()
    rows = (
        _json(output)
        if output.startswith("[")
        else [_json(line) for line in output.splitlines() if line.strip()]
    )
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RuntimeError(_DOCKER_ERROR)
    if not rows:
        return "실행 중이거나 중지된 서버 컨테이너가 없습니다. ‘서버 시작’을 눌러 시작하세요."
    lines = ["선택한 저장 위치의 서버 상태입니다."]
    for row in rows:
        name = _SERVICES.get(str(row.get("Service", "")), "알 수 없는 서비스")
        state = _STATES.get(str(row.get("State", "")).lower(), "상태 확인 필요")
        health = _HEALTH.get(str(row.get("Health", "")).lower(), "")
        lines.append(f"• {name}: {state}" + (f" / {health}" if health else ""))
    lines.append("서비스 실행 상태이며 실제 카메라 영상·감지·녹화는 별도로 확인하세요.")
    return "\n".join(lines)


def run_service_action(
    server_dir: Path,
    installed: Installation,
    action: str,
    progress: Callable[[str], None],
) -> str:
    """현재 Data의 마운트가 선택한 배포와 같을 때만 서버를 변경한다."""

    if action not in {"start", "restart", "stop", "status"}:
        raise ValueError("지원하지 않는 서버 작업입니다.")
    deadline = time.monotonic() + (
        START_TIMEOUT_SECONDS if action == "start" else ACTION_TIMEOUT_SECONDS
    )
    adapter = ComposeAdapter(server_dir, installed.env_file)
    progress("저장된 설정과 현재 Docker 서버의 저장 위치를 확인하고 있습니다…")
    state = _deployment_state(adapter, installed, deadline)
    if state in {"different", "unknown"}:
        message = _OTHER_DEPLOYMENT if state == "different" else _UNKNOWN_DEPLOYMENT
        if action == "status":
            return message
        raise RuntimeError(message)
    if state == "empty" and action != "start":
        return "선택한 설정의 서버 컨테이너가 없습니다. ‘서버 시작’을 눌러 시작하세요."
    if action == "status":
        return _status(adapter, deadline)
    if action == "start":
        progress("모델·인증서와 Docker 실행 준비를 확인하고 있습니다…")
        try:
            failures = [
                item for item in adapter.deployment_prerequisites() if not item.ok
            ]
        except (OSError, UnicodeError, ValueError, subprocess.SubprocessError):
            raise RuntimeError(_DOCKER_ERROR) from None
        if failures:
            raise RuntimeError(
                "서버 파일·모델·HTTPS 인증서 또는 Docker 실행 준비가 부족합니다. "
                "설치 파일과 Docker Desktop을 확인하세요. 설정과 인증키는 유지됩니다."
            )
        progress(
            "서버 이미지를 준비하고 실행하고 있습니다. 처음에는 시간이 걸릴 수 있습니다…"
        )
        arguments = START_ARGUMENTS
    elif action == "restart":
        progress("선택한 저장 위치의 서버를 재시작하고 있습니다…")
        arguments = ("restart",)
    else:
        progress("선택한 저장 위치의 서버를 중지하고 있습니다…")
        arguments = ("down",)
    _run(
        _command(adapter, *arguments),
        directory=adapter.server_dir,
        deadline=deadline,
        capture=False,
    )
    if action == "stop":
        return "서버를 중지했습니다. 영상·설정·인증키는 저장 위치에 유지됩니다."
    progress("서버 실행 결과를 확인하고 있습니다…")
    return _status(adapter, deadline)
