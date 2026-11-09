"""선택한 저장소와 실행 중인 배포를 대조한 뒤 제한 시간 안에 관리한다."""

from __future__ import annotations

import json
import os
import posixpath
import re
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from server.deployment.context import DeploymentContext
from server.deployment.validation import deployment_path, read_deployment_env

from .compose import (
    START_ARGUMENTS,
    STOP_ARGUMENTS,
    ComposeAdapter,
    migrate_detection_model,
)
from .local_camera import stop_publisher


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
    "nginx": "HTTP(S) 접속",
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
_ANALYSIS_STARTUP_LABELS = {
    "ANALYSIS_MODEL_MISSING": "모델 파일 없음",
    "ANALYSIS_MODEL_INVALID": "모델을 읽을 수 없음",
    "ANALYSIS_MODEL_STARTUP_TIMEOUT": "모델 준비 시간 초과",
    "ANALYSIS_PLUGIN_INVALID": "PAR 분석기 설정 오류",
    "ANALYSIS_STARTUP_FAILED": "분석기 초기화 실패",
}


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
    output_file = None if capture else tempfile.TemporaryFile(
        mode="w+t", encoding="utf-8", errors="replace"
    )
    options: dict[str, Any] = {
        "cwd": directory,
        "stdout": subprocess.PIPE if capture else output_file,
        "stderr": subprocess.PIPE if capture else subprocess.STDOUT,
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
        if output_file is not None:
            output_file.close()
        raise RuntimeError(_DOCKER_ERROR) from None
    try:
        stdout, stderr = process.communicate(
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
    finally:
        if output_file is not None:
            output_file.flush()
            output_file.seek(0, os.SEEK_END)
            output_size = output_file.tell()
            output_file.seek(max(0, output_size - 8192))
            file_output = output_file.read()
            output_file.close()
    command_output = file_output if not capture else "\n".join(
        part for part in (stdout, stderr) if isinstance(part, str) and part
    )
    if process.returncode:
        package = re.search(
            r"No matching distribution found for ([A-Za-z0-9_.+-]+)",
            command_output or "",
        )
        if package:
            raise RuntimeError(
                f"Docker operation failed (exit {process.returncode}): "
                f"Python package {package.group(1)} is unavailable from the configured index."
            )
        safe_lines = []
        for line in (command_output or "").splitlines()[-8:]:
            if re.search(
                r"(?i)(?:DATA_[A-Z0-9_]*(?:TOKEN|PASSWORD|SECRET)|JWT_SECRET|INTERNAL_SERVICE_TOKEN|MEDIA_READ_PASSWORD)",
                line,
            ):
                line = "[sensitive command output redacted]"
            line = re.sub(
                r"(?i)(rtsp|rtsps|https?)://[^\s/@]+@",
                r"\1://<redacted>@",
                line,
            )
            safe_lines.append(line[-300:])
        summary = "\n".join(safe_lines).strip()
        raise RuntimeError(
            f"Docker operation failed (exit {process.returncode})."
            + (f"\n{summary}" if summary else " See Docker Desktop for command details.")
        )
    return (stdout or "") if capture else ""


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


# Data 컨테이너가 없을 때 남은 컨테이너(예: AI 프로파일 서비스)의 마운트로 동일 배포 여부를 판별한다.
def _partial_mounts_match(mounts: Any, expected_any: dict[str, Path]) -> str:
    if not isinstance(mounts, list):
        return "unknown"
    matched_targets = 0
    for mount in mounts:
        if not isinstance(mount, dict):
            continue
        target = mount.get("Destination")
        if target not in expected_any:
            continue
        matched_targets += 1
        actual = mount.get("Source")
        if (
            mount.get("Type") != "bind"
            or not isinstance(actual, str)
            or _host_path(actual) != _host_path(str(expected_any[target]))
        ):
            return "different"
    return "matching" if matched_targets > 0 else "unknown"


# Compose 프로젝트명만 믿지 않고 Data의 설정·DB 마운트로 선택한 설치와의 일치를 확인한다.
def _deployment_state(
    adapter: ComposeAdapter, installed: DeploymentContext, deadline: float
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
    expected_any: dict[str, Path] = dict(expected)
    for env_key, target in (
        ("SNAPSHOTS_DIR", "/snapshots"),
        ("RECORDINGS_DIR", "/recordings"),
        ("MODELS_DIR", "/models"),
    ):
        raw_value = values.get(env_key)
        if raw_value:
            try:
                expected_any[target] = deployment_path(adapter.server_dir, raw_value)
            except (OSError, ValueError):
                pass
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
    data_containers = _ids(
        _run(
            [*query, "--filter", "label=com.docker.compose.service=data"],
            directory=adapter.server_dir,
            deadline=deadline,
        )
    )
    if not data_containers:
        mounts_output = _run(
            ["docker", "inspect", "--format", "{{json .Mounts}}", *containers],
            directory=adapter.server_dir,
            deadline=deadline,
        )
        lines = [line for line in mounts_output.splitlines() if line.strip()]
        if len(lines) != len(containers):
            return "unknown"
        statuses = [_partial_mounts_match(_json(line), expected_any) for line in lines]
        if "different" in statuses:
            return "different"
        if all(status == "matching" for status in statuses):
            return "matching"
        return "unknown"
    if not set(data_containers).issubset(containers):
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


def _compose_ps_rows(adapter: ComposeAdapter, deadline: float) -> list[dict[str, Any]]:
    output = _run(
        _command(adapter, "ps", "--all", "--format", "json"),
        directory=adapter.server_dir,
        deadline=deadline,
    ).strip()
    if not output:
        return []
    rows = (
        _json(output)
        if output.startswith("[")
        else [_json(line) for line in output.splitlines() if line.strip()]
    )
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise RuntimeError(_DOCKER_ERROR)
    return rows


def get_optional_service_states(
    server_dir: Path,
    installed: DeploymentContext,
) -> dict[str, Any]:
    """선택한 배포의 preprocessing·analysis 컨테이너가 실행 중인지 반환한다."""

    states: dict[str, Any] = {"preprocessing": False, "analysis": False}
    deadline = time.monotonic() + QUERY_TIMEOUT_SECONDS
    adapter = ComposeAdapter(server_dir, installed.env_file)
    try:
        if _deployment_state(adapter, installed, deadline) != "matching":
            return states
        rows = _compose_ps_rows(adapter, deadline)
    except (OSError, UnicodeError, ValueError, RuntimeError):
        return states
    for row in rows:
        service = str(row.get("Service", ""))
        raw_state = str(row.get("State", "")).lower()
        if service in {"preprocessing", "analysis"} and raw_state in {
            "running",
            "restarting",
        }:
            states[service] = True
        if service == "analysis" and raw_state in {"exited", "restarting", "dead"}:
            message = _analysis_stopped_status(adapter, deadline)
            if message:
                states["analysis_message"] = message
    return states


# 알려진 서비스·상태 이름만 표시해 Docker 응답에 포함될 수 있는 임의 값 노출을 제한한다.
def _status(adapter: ComposeAdapter, deadline: float) -> str:
    rows = _compose_ps_rows(adapter, deadline)
    if not rows:
        return "실행 중이거나 중지된 서버 컨테이너가 없습니다. ‘서버 시작’을 눌러 시작하세요."
    lines = ["선택한 저장 위치의 서버 상태입니다."]
    for row in rows:
        service = str(row.get("Service", ""))
        raw_state = str(row.get("State", "")).lower()
        name = _SERVICES.get(service, "알 수 없는 서비스")
        state = _STATES.get(raw_state, "상태 확인 필요")
        if service == "analysis" and raw_state in {"exited", "restarting", "dead"}:
            state = _analysis_stopped_status(adapter, deadline) or state
        health = _HEALTH.get(str(row.get("Health", "")).lower(), "")
        lines.append(f"• {name}: {state}" + (f" / {health}" if health else ""))
    lines.append("서비스 실행 상태이며 실제 카메라 영상·감지·녹화는 별도로 확인하세요.")
    return "\n".join(lines)


def _analysis_stopped_status(adapter: ComposeAdapter, deadline: float) -> str | None:
    """컨테이너 로그에서 허용한 분석 준비 오류 코드만 관제용 상태로 바꾼다."""

    try:
        output = _run(
            _command(adapter, "logs", "--tail", "100", "analysis"),
            directory=adapter.server_dir,
            deadline=deadline,
        )
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return None
    for code, label in _ANALYSIS_STARTUP_LABELS.items():
        if code in output:
            return f"{label} · 분석 중지"
    return None


def _analysis_startup_failure_message(output: str) -> str | None:
    for code, label in _ANALYSIS_STARTUP_LABELS.items():
        if code in output:
            return f"이벤트 분석을 시작하지 못했습니다: {label}. 모델과 설정을 확인하세요."
    return None


_CLEARABLE_DB_TABLES = (
    "event_recording_segments",
    "object_jobs",
    "push_deliveries",
    "events",
    "recording_segments",
    "recovery_jobs",
    "person_identity_links",
    "person_track_presence",
    "identity_gallery",
    "global_persons",
    "live_objects",
    "maintenance_cursors",
)
_CLEARABLE_DB_SEQUENCES = (
    "events",
    "recording_segments",
    "object_jobs",
    "push_deliveries",
    "recovery_jobs",
    "identity_gallery",
    "global_persons",
)


def _format_bytes(size_bytes: int) -> str:
    size = float(max(0, size_bytes))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024.0 or unit == "GB":
            return f"{int(size)} B" if unit == "B" else f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size:.2f} GB"


def _empty_directory_contents(directory: Path) -> tuple[int, int]:
    """루트 디렉터리 자체는 유지하고 내부 파일·하위 폴더만 모두 삭제한다."""

    if not directory.is_dir():
        return 0, 0
    deleted_files = 0
    freed_bytes = 0
    for child in list(directory.iterdir()):
        if child.is_symlink() or child.is_file():
            try:
                freed_bytes += child.stat().st_size
            except OSError:
                pass
            child.unlink(missing_ok=True)
            deleted_files += 1
        elif child.is_dir():
            for nested in child.rglob("*"):
                if nested.is_symlink() or nested.is_file():
                    try:
                        freed_bytes += nested.stat().st_size
                    except OSError:
                        pass
                    deleted_files += 1
            shutil.rmtree(child)
    return deleted_files, freed_bytes


def _clear_database_logs(db_path: Path) -> tuple[int, int]:
    """사용자·카메라 설정은 보존하고 이벤트·녹화·인물식별 이력 테이블과 시퀀스를 초기화한다."""

    if not db_path.is_file():
        return 0, 0
    deleted_events = 0
    deleted_segments = 0
    connection = sqlite3.connect(db_path, timeout=10.0, isolation_level=None)
    try:
        connection.execute("PRAGMA busy_timeout = 5000;")
        connection.execute("PRAGMA foreign_keys = ON;")
        existing_tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if "events" in existing_tables:
            row = connection.execute("SELECT COUNT(*) FROM events").fetchone()
            deleted_events = int(row[0] or 0) if row else 0
        if "recording_segments" in existing_tables:
            row = connection.execute(
                "SELECT COUNT(*) FROM recording_segments"
            ).fetchone()
            deleted_segments = int(row[0] or 0) if row else 0
        connection.execute("BEGIN IMMEDIATE;")
        try:
            for table in _CLEARABLE_DB_TABLES:
                if table in existing_tables:
                    connection.execute(f"DELETE FROM {table}")
            if "camera_runtime_status" in existing_tables:
                columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(camera_runtime_status)"
                    ).fetchall()
                }
                assignments = [
                    f"{col} = NULL"
                    for col in ("event_cursor", "last_error_code")
                    if col in columns
                ]
                if assignments:
                    connection.execute(
                        f"UPDATE camera_runtime_status SET {', '.join(assignments)}"
                    )
            if "cameras" in existing_tables:
                columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(cameras)"
                    ).fetchall()
                }
                if "recording_last_error_code" in columns:
                    connection.execute(
                        "UPDATE cameras SET recording_last_error_code = NULL"
                    )
            if "sqlite_sequence" in existing_tables:
                placeholders = ", ".join("?" for _ in _CLEARABLE_DB_SEQUENCES)
                connection.execute(
                    f"DELETE FROM sqlite_sequence WHERE name IN ({placeholders})",
                    _CLEARABLE_DB_SEQUENCES,
                )
            connection.execute("COMMIT;")
        except Exception:
            connection.execute("ROLLBACK;")
            raise
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        connection.execute("VACUUM;")
    finally:
        connection.close()
    return deleted_events, deleted_segments


def _clear_media_and_event_logs(
    adapter: ComposeAdapter,
    installed: DeploymentContext,
    *,
    state: str,
    deadline: float,
    progress: Callable[[str], None],
) -> str:
    running_core = False
    running_optional: list[str] = []
    if state == "matching":
        rows = _compose_ps_rows(adapter, deadline)
        active_services = {
            str(row.get("Service", ""))
            for row in rows
            if str(row.get("State", "")).lower() in {"running", "restarting"}
        }
        running_core = bool(active_services)
        running_optional = [
            service
            for service in ("preprocessing", "analysis")
            if service in active_services
        ]
    try:
        stop_publisher(installed.data_root)
    except OSError:
        pass
    if state == "matching":
        progress("로그 및 영상 정리를 위해 실행 중인 서버 컨테이너를 일시 중지하고 있습니다…")
        _run(
            _command(adapter, *STOP_ARGUMENTS),
            directory=adapter.server_dir,
            deadline=deadline,
            capture=False,
        )
    try:
        values = read_deployment_env(installed.env_file)
    except (OSError, UnicodeError, ValueError):
        values = {}

    def _storage_dir(env_key: str, default_subdir: str) -> Path:
        raw = values.get(env_key)
        if raw:
            return deployment_path(adapter.server_dir, raw)
        return (installed.data_root / default_subdir).resolve()

    recordings_dir = _storage_dir("RECORDINGS_DIR", "recordings")
    recovered_dir = _storage_dir("RECOVERED_DIR", "recovered")
    snapshots_dir = _storage_dir("SNAPSHOTS_DIR", "snapshots")
    logs_dir = _storage_dir("LOGS_DIR", "logs")
    database_dir = _storage_dir("DATABASE_DIR", "database")

    progress("저장된 녹화 영상, 복구 영상, 스냅샷 및 로그 파일을 삭제하고 있습니다…")
    deleted_files = 0
    freed_bytes = 0
    for target_dir in (recordings_dir, recovered_dir, snapshots_dir, logs_dir):
        files_count, bytes_count = _empty_directory_contents(target_dir)
        deleted_files += files_count
        freed_bytes += bytes_count

    progress("데이터베이스의 이벤트·녹화·인물식별 이력을 초기화하고 있습니다…")
    deleted_events, deleted_segments = _clear_database_logs(
        database_dir / "ai_cctv.db"
    )

    if running_core:
        progress("서버 컨테이너를 다시 시작하고 있습니다…")
        _run(
            _command(adapter, "up", "-d", "--wait"),
            directory=adapter.server_dir,
            deadline=deadline,
            capture=False,
        )
        for service in running_optional:
            progress(f"{service} 컨테이너를 다시 시작하고 있습니다…")
            _run(
                _command(adapter, "up", "-d", "--wait", "--no-deps", service),
                directory=adapter.server_dir,
                deadline=deadline,
                capture=False,
            )

    summary = (
        "영상 및 이벤트 로그 비우기를 완료했습니다.\n"
        f"• 삭제된 이벤트 이력: {deleted_events}건\n"
        f"• 삭제된 녹화 세그먼트 이력: {deleted_segments}건\n"
        f"• 삭제된 미디어·스냅샷·로그 파일: {deleted_files}개 ({_format_bytes(freed_bytes)})"
    )
    if running_core:
        return f"{summary}\n\n{_status(adapter, deadline)}"
    return f"{summary}\n• 서버 상태: 중지됨 (관리자 계정·카메라 등록 정보·설정은 유지됨)"


def clear_media_and_event_logs(
    server_dir: Path,
    installed: DeploymentContext,
    progress: Callable[[str], None],
) -> str:
    """저장된 녹화·복구·스냅샷 파일과 DB 이벤트·녹화·인물식별 이력을 비운다."""

    return run_service_action(server_dir, installed, "clear-logs", progress)


def run_service_action(
    server_dir: Path,
    installed: DeploymentContext,
    action: str,
    progress: Callable[[str], None],
) -> str:
    """현재 Data의 마운트가 선택한 배포와 같을 때만 서버를 변경한다."""

    optional = action in {
        "preprocessing-on", "preprocessing-off", "analysis-on", "analysis-off"
    }
    if action not in {"start", "restart", "stop", "status", "clear-logs"} and not optional:
        raise ValueError("지원하지 않는 서버 작업입니다.")
    deadline = time.monotonic() + (
        START_TIMEOUT_SECONDS
        if action in {"start", "clear-logs"} or action.endswith("-on")
        else ACTION_TIMEOUT_SECONDS
    )
    adapter = ComposeAdapter(server_dir, installed.env_file)
    migrated_model = False
    if action in {"start", "restart", "preprocessing-on", "analysis-on"}:
        try:
            migrated_model = migrate_detection_model(installed.env_file)
        except (OSError, UnicodeError, ValueError):
            raise RuntimeError("감지 모델 설정을 갱신하지 못했습니다.") from None
    progress("저장된 설정과 현재 Docker 서버의 저장 위치를 확인하고 있습니다…")
    state = _deployment_state(adapter, installed, deadline)
    if state in {"different", "unknown"}:
        message = _OTHER_DEPLOYMENT if state == "different" else _UNKNOWN_DEPLOYMENT
        if action == "status":
            return message
        raise RuntimeError(message)
    if action == "clear-logs":
        return _clear_media_and_event_logs(
            adapter,
            installed,
            state=state,
            deadline=deadline,
            progress=progress,
        )
    if state == "empty" and action != "start":
        return "선택한 설정의 서버 컨테이너가 없습니다. ‘서버 시작’을 눌러 시작하세요."
    if action == "status":
        return _status(adapter, deadline)
    if optional:
        service, operation = action.rsplit("-", 1)
        if operation == "on" and service in {"preprocessing", "analysis"}:
            failures = [
                item
                for item in adapter.deployment_prerequisites(
                    require_ai_models=True,
                    require_analysis_model=(service == "analysis"),
                )
                if not item.ok
            ]
            if failures:
                header = (
                    "이벤트 분석을 켜려면 필요한 탐지 모델·OSNet 모델·PAR 속성 분석 모델(par_serving_exp3.pt) 파일을 준비해야 합니다.\n"
                    if service == "analysis"
                    else "AI 처리를 켜려면 필요한 탐지 모델과 OSNet 모델 파일을 준비해야 합니다.\n"
                )
                raise RuntimeError(
                    header + "\n".join(item.message for item in failures)
                )
        progress(f"{service} {'시작' if operation == 'on' else '중지'} 중…")
        commands = []
        if action == "analysis-on":
            commands.append(("up", "-d", "--build", "--wait", "preprocessing"))
        if action == "preprocessing-off":
            commands.append(("stop", "analysis"))
        commands.append(
            ("up", "-d", "--build", "--wait", "--no-deps", service)
            if operation == "on"
            else ("stop", service)
        )
        for arguments in commands:
            try:
                _run(
                    _command(adapter, *arguments),
                    directory=adapter.server_dir,
                    deadline=deadline,
                    capture=False,
                )
            except RuntimeError as exc:
                message = (
                    _analysis_startup_failure_message(str(exc))
                    if service == "analysis"
                    else None
                )
                if message:
                    raise RuntimeError(message) from None
                raise
        return f"{service} {'활성화' if operation == 'on' else '비활성화'} 요청을 적용했습니다. 상태를 확인하세요."
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
        arguments = START_ARGUMENTS if migrated_model else ("restart",)
    else:
        progress("선택한 저장 위치의 서버를 중지하고 있습니다…")
        arguments = STOP_ARGUMENTS
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
