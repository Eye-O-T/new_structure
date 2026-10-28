"""Run one FFmpeg publisher until explicitly stopped."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

from server.operations.local_camera import (
    resolve_ffmpeg_executable,
    write_runtime_state,
)


def _ffmpeg_path() -> str:
    value = resolve_ffmpeg_executable()
    if not value:
        raise RuntimeError(
            "FFmpeg를 찾을 수 없습니다. AI_CCTV_FFMPEG 환경 변수 또는 PATH를 확인하세요."
        )
    return value


def _sanitize_error(text: str) -> str:
    cleaned = re.sub(
        r"(rtsp|http|https)://[^\s\"']+",
        r"\1://<redacted>",
        text or "",
        flags=re.IGNORECASE,
    )
    lines = [line.strip() for line in cleaned.splitlines() if line.strip()]
    return lines[-1][:200] if lines else ""


def _build_command(
    ffmpeg: str,
    *,
    device: str,
    stream_url: str,
    width: int,
    height: int,
    fps: int,
    explicit_dshow_mode: bool,
) -> list[str]:
    gop = max(1, fps)
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "dshow"]
    if explicit_dshow_mode:
        cmd += [
            "-video_size",
            f"{width}x{height}",
            "-framerate",
            str(fps),
        ]
    cmd += ["-i", f"video={device}", "-an"]
    if not explicit_dshow_mode:
        cmd += ["-vf", f"scale={width}:{height},hflip", "-r", str(fps)]
    else:
        cmd += ["-vf", "hflip"]
    cmd += [
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-tune",
        "zerolatency",
        "-g",
        str(gop),
        "-keyint_min",
        str(gop),
        "-sc_threshold",
        "0",
        "-bf",
        "0",
        "-pix_fmt",
        "yuv420p",
        "-f",
        "rtsp",
        "-rtsp_transport",
        "tcp",
        stream_url,
    ]
    return cmd


def _record_error(error_path: Path | None, message: str) -> None:
    if error_path is None or not message:
        return
    try:
        error_path.parent.mkdir(parents=True, exist_ok=True)
        error_path.write_text(_sanitize_error(message), encoding="utf-8")
    except OSError:
        pass


def _read_stderr_log(log_path: Path) -> str:
    try:
        if log_path.is_file():
            return log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    return ""


def main() -> int:
    error_path: Path | None = None
    stderr_log: Path | None = None
    stderr_handle = None
    try:
        payload = json.load(sys.stdin)
        state = Path(payload["state_path"]).expanduser().resolve()
        stderr_log = state.with_suffix(".stderr.log")
        raw_error_path = payload.get("error_path")
        if raw_error_path:
            error_path = Path(str(raw_error_path)).expanduser().resolve()
        device = str(payload["device_name"])
        camera_id = str(payload["camera_id"])
        stream_url = str(payload["stream_url"])
        width = int(payload.get("width", 1280))
        height = int(payload.get("height", 720))
        fps = int(payload.get("fps", 30))
        ffmpeg = _ffmpeg_path()

        state.parent.mkdir(parents=True, exist_ok=True)
        stderr_handle = stderr_log.open("w", encoding="utf-8", errors="replace")
        popen_kwargs = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": stderr_handle,
        }
        if os.name == "nt":
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)

        command = _build_command(
            ffmpeg,
            device=device,
            stream_url=stream_url,
            width=width,
            height=height,
            fps=fps,
            explicit_dshow_mode=True,
        )
        child = subprocess.Popen(command, **popen_kwargs)
        time.sleep(0.4)
        if child.poll() is not None:
            # Retry with device default DirectShow pin format if 1280x720@30fps was rejected
            stderr_handle.seek(0)
            stderr_handle.truncate()
            fallback_cmd = _build_command(
                ffmpeg,
                device=device,
                stream_url=stream_url,
                width=width,
                height=height,
                fps=fps,
                explicit_dshow_mode=False,
            )
            child = subprocess.Popen(fallback_cmd, **popen_kwargs)
            time.sleep(0.4)
            if child.poll() is not None:
                stderr_handle.flush()
                stderr_text = _read_stderr_log(stderr_log)
                _record_error(
                    error_path,
                    stderr_text or "카메라 장치를 열거나 RTSP 서버에 연결하지 못했습니다.",
                )
                return child.returncode or 1

        del payload, stream_url, command
        write_runtime_state(
            state,
            {
                "helper_pid": os.getpid(),
                "ffmpeg_pid": child.pid,
                "camera_id": camera_id,
                "device_name": device,
            },
        )

        def terminate(_signum, _frame):
            if child.poll() is None:
                child.terminate()

        signal.signal(signal.SIGTERM, terminate)
        signal.signal(signal.SIGINT, terminate)
        rc = child.wait()
        if rc != 0:
            stderr_handle.flush()
            _record_error(error_path, _read_stderr_log(stderr_log))
        return rc
    except Exception as exc:
        _record_error(error_path, str(exc))
        return 1
    finally:
        if stderr_handle is not None:
            try:
                stderr_handle.close()
            except OSError:
                pass
        if stderr_log is not None:
            stderr_log.unlink(missing_ok=True)
        if "state" in locals():
            state.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
