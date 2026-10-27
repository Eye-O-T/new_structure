"""Manage the detached Windows local-camera publisher process."""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from server.deployment.private_files import restrict_private_file

_COMMON_FFMPEG_PATHS = (
    Path(__file__).resolve().parents[2] / ".tools" / "ffmpeg" / "ffmpeg.exe",
    Path.home()
    / "Downloads"
    / "ffmpeg-8.0.1-essentials_build"
    / "ffmpeg-8.0.1-essentials_build"
    / "bin"
    / "ffmpeg.exe",
)


def resolve_ffmpeg_executable() -> str | None:
    """Locate FFmpeg from AI_CCTV_FFMPEG, PATH, or bundled workspace tools."""

    configured = os.getenv("AI_CCTV_FFMPEG")
    if configured and Path(configured).expanduser().is_file():
        return str(Path(configured).expanduser().resolve())
    found = shutil.which("ffmpeg")
    if found:
        return found
    for candidate in _COMMON_FFMPEG_PATHS:
        if candidate.is_file():
            return str(candidate.resolve())
    return None


def list_camera_devices() -> list[str]:
    """Return the exact DirectShow video device names reported by FFmpeg."""

    ffmpeg = resolve_ffmpeg_executable()
    if not ffmpeg:
        return []
    try:
        result = subprocess.run(
            [ffmpeg, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
            capture_output=True,
            timeout=5,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0,
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


def error_state_path(data_root: Path) -> Path:
    return data_root.expanduser().resolve() / "runtime" / "local-camera.error"


def settings_path(data_root: Path) -> Path:
    return data_root.expanduser().resolve() / "config" / "local-camera.json"


def _read_json(path: Path) -> dict[str, Any]:
    for attempt in range(3):
        try:
            if not path.is_file():
                return {}
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except PermissionError:
            if attempt < 2:
                time.sleep(0.02)
                continue
            return {}
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
    return {}


def _pid_running(pid: object) -> bool:
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        kernel32 = ctypes.windll.kernel32
        # SYNCHRONIZE (0x00100000) | PROCESS_QUERY_LIMITED_INFORMATION (0x1000)
        handle = kernel32.OpenProcess(0x00101000, False, pid)
        if handle:
            try:
                # WAIT_TIMEOUT (258) means the process has not terminated.
                return int(kernel32.WaitForSingleObject(handle, 0)) == 258
            finally:
                kernel32.CloseHandle(handle)
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong(0)
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return int(exit_code.value) == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def publisher_status(data_root: Path) -> dict[str, Any]:
    path = state_path(data_root)
    state = _read_json(path)
    if not state:
        return {"status": "stopped"}
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


def _publisher_env() -> dict[str, str]:
    env = os.environ.copy()
    repo_root = Path(__file__).resolve().parents[2]
    extra_paths = [str(repo_root), str(repo_root / "lib")]
    existing = env.get("PYTHONPATH", "")
    if existing:
        extra_paths.append(existing)
    env["PYTHONPATH"] = os.pathsep.join(extra_paths)
    ffmpeg = resolve_ffmpeg_executable()
    if ffmpeg and not env.get("AI_CCTV_FFMPEG"):
        env["AI_CCTV_FFMPEG"] = ffmpeg
    return env


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
    err_path = error_state_path(data_root)
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
        err_path.unlink(missing_ok=True)
        payload = {
            "state_path": str(path),
            "error_path": str(err_path),
            "device_name": device_name,
            "camera_id": camera_id,
            "stream_url": stream_url,
            "width": width,
            "height": height,
            "fps": fps,
        }
        repo_root = Path(__file__).resolve().parents[2]
        options: dict[str, Any] = {
            "stdin": subprocess.PIPE,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
            "text": True,
            "encoding": "utf-8",
            "close_fds": True,
            "env": _publisher_env(),
        }
        if repo_root.is_dir():
            options["cwd"] = str(repo_root)
        if os.name == "nt":
            # DETACHED_PROCESS and CREATE_NO_WINDOW are mutually exclusive in Win32;
            # CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW runs headless without console errors.
            options["creationflags"] = (
                getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
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
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status = publisher_status(data_root)
            if status["status"] == "running":
                save_device_setting(data_root, device_name)
                return status
            if process.poll() is not None:
                break
            time.sleep(0.05)
        detail = ""
        try:
            if err_path.is_file():
                detail = err_path.read_text(encoding="utf-8", errors="replace").strip()
                err_path.unlink(missing_ok=True)
        except OSError:
            detail = ""
        if detail:
            raise RuntimeError(f"로컬 카메라 송출 프로세스를 시작하지 못했습니다: {detail}")
        raise RuntimeError("로컬 카메라 송출 프로세스를 시작하지 못했습니다.")
    finally:
        lock_path.unlink(missing_ok=True)


def stop_publisher(data_root: Path) -> bool:
    path = state_path(data_root)
    state = _read_json(path)
    pids = [
        pid
        for pid in (state.get("ffmpeg_pid"), state.get("helper_pid"))
        if _pid_running(pid)
    ]
    if not pids:
        path.unlink(missing_ok=True)
        return False
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(_pid_running(pid) for pid in pids):
        time.sleep(0.05)
    if os.name == "nt" and any(_pid_running(pid) for pid in pids):
        for pid in pids:
            if _pid_running(pid):
                try:
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/T", "/F"],
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=5,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    )
                except (OSError, subprocess.SubprocessError):
                    pass
    if any(_pid_running(pid) for pid in pids):
        raise OSError("로컬 카메라 송출 프로세스 종료를 확인하지 못했습니다.")
    path.unlink(missing_ok=True)
    return True


def write_runtime_state(path: Path, value: dict[str, Any]) -> None:
    """Atomically write non-secret publisher process metadata."""

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    try:
        restrict_private_file(temporary)
    except OSError:
        pass
    os.replace(temporary, path)
