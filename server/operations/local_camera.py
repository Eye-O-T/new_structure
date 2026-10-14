"""Manage the detached Windows local-camera publisher process."""

from __future__ import annotations

import json
import os
import signal
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from server.deployment.private_files import restrict_private_file


def list_camera_devices() -> list[str]:
    """Return the exact DirectShow video device names reported by FFmpeg."""

    ffmpeg = os.getenv("AI_CCTV_FFMPEG") or shutil.which("ffmpeg")
    if not ffmpeg:
        return []
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    stderr = (result.stderr or b"").decode("utf-8", errors="replace")
    devices: list[str] = []
    for line in stderr.splitlines():
        match = re.search(r'"([^"]+)"\s+\((?:video|none)\)\s*$', line, re.IGNORECASE)
        if match and match.group(1) not in devices:
            devices.append(match.group(1))
    return devices


def state_path(data_root: Path) -> Path:
    return data_root.expanduser().resolve() / "runtime" / "local-camera.json"


def settings_path(data_root: Path) -> Path:
    return data_root.expanduser().resolve() / "config" / "local-camera.json"


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _pid_running(pid: object) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return result.returncode == 0 and f'"{pid}"' in result.stdout
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def publisher_status(data_root: Path) -> dict[str, Any]:
    path = state_path(data_root)
    state = _read_json(path)
    running = _pid_running(state.get("helper_pid")) and _pid_running(
        state.get("ffmpeg_pid")
    )
    if not running:
        path.unlink(missing_ok=True)
        return {"status": "stopped"}
    return {
        "status": "running",
        "pid": state["ffmpeg_pid"],
        "camera_id": str(state.get("camera_id", "")),
        "device_name": str(state.get("device_name", "")),
    }


def save_device_setting(data_root: Path, device_name: str) -> None:
    path = settings_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"device_name": device_name}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def load_device_setting(data_root: Path) -> str | None:
    value = _read_json(settings_path(data_root)).get("device_name")
    return value if isinstance(value, str) and value.strip() else None


def _publisher_command() -> list[str]:
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).with_name("AI_CCTV_Local_Camera.exe"))]
    return [sys.executable, "-m", "server.local_camera_publisher"]


def start_publisher(
    data_root: Path,
    *,
    device_name: str,
    camera_id: str,
    stream_url: str,
    width: int = 1280,
    height: int = 720,
    fps: int = 30,
) -> dict[str, Any]:
    path = state_path(data_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        raise RuntimeError("로컬 카메라 송출 시작 작업이 이미 진행 중입니다.") from None
    try:
        os.close(descriptor)
        current = publisher_status(data_root)
        if current["status"] == "running":
            if current.get("camera_id") == camera_id and current.get("device_name") == device_name:
                return current
            raise RuntimeError("다른 로컬 카메라 송출이 이미 실행 중입니다. 먼저 중지하세요.")
        payload = {
            "state_path": str(path),
            "device_name": device_name,
            "camera_id": camera_id,
            "stream_url": stream_url,
            "width": width,
            "height": height,
            "fps": fps,
        }
        options: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "text": True,
            "encoding": "utf-8",
            "close_fds": True,
        }
        if os.name == "nt":
            options["creationflags"] = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                | getattr(subprocess, "DETACHED_PROCESS", 0)
                | getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
        else:
            options["start_new_session"] = True
        process = subprocess.Popen(_publisher_command(), **options)
        assert process.stdin is not None
        process.stdin.write(json.dumps(payload, ensure_ascii=False))
        process.stdin.close()
        # The secret-bearing URL is never persisted by the controller.
        del payload, stream_url
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            status = publisher_status(data_root)
            if status["status"] == "running":
                save_device_setting(data_root, device_name)
                return status
            if process.poll() is not None:
                break
            time.sleep(0.05)
        raise RuntimeError("로컬 카메라 송출 프로세스를 시작하지 못했습니다.")
    finally:
        lock_path.unlink(missing_ok=True)


def stop_publisher(data_root: Path) -> bool:
    path = state_path(data_root)
    state = _read_json(path)
    pids = [
        pid
        for pid in (state.get("helper_pid"), state.get("ffmpeg_pid"))
        if _pid_running(pid)
    ]
    if not pids:
        path.unlink(missing_ok=True)
        return False
    if os.name == "nt":
        for pid in pids:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=10,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
    else:
        for pid in pids:
            if _pid_running(pid):
                os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(_pid_running(pid) for pid in pids):
        time.sleep(0.05)
    if any(_pid_running(pid) for pid in pids):
        raise OSError("로컬 카메라 송출 프로세스 종료를 확인하지 못했습니다.")
    path.unlink(missing_ok=True)
    return True


def write_runtime_state(path: Path, value: dict[str, Any]) -> None:
    """Atomically write non-secret publisher process metadata."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    restrict_private_file(temporary)
    os.replace(temporary, path)
    restrict_private_file(path)
