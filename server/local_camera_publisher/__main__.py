"""Run one FFmpeg publisher until explicitly stopped."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from server.operations.local_camera import write_runtime_state


def _ffmpeg_path() -> str:
    value = os.getenv("AI_CCTV_FFMPEG") or shutil.which("ffmpeg")
    if not value:
        raise RuntimeError("FFmpeg was not found")
    return value


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        state = Path(payload["state_path"]).expanduser().resolve()
        device = str(payload["device_name"])
        camera_id = str(payload["camera_id"])
        stream_url = str(payload["stream_url"])
        fps = int(payload["fps"])
        gop = max(1, fps)
        command = [
            _ffmpeg_path(), "-hide_banner", "-loglevel", "warning", "-f", "dshow",
            "-video_size", f"{int(payload['width'])}x{int(payload['height'])}",
            "-framerate", str(fps), "-i", f"video={device}", "-an", "-c:v", "libx264",
            "-preset", "veryfast", "-tune", "zerolatency", "-g", str(gop),
            "-keyint_min", str(gop), "-sc_threshold", "0", "-bf", "0",
            "-pix_fmt", "yuv420p", "-f", "rtsp", "-rtsp_transport", "tcp", stream_url,
        ]
        child = subprocess.Popen(command, stdin=subprocess.DEVNULL)
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
        return child.wait()
    except (KeyError, TypeError, ValueError, OSError, json.JSONDecodeError):
        return 1
    finally:
        if "state" in locals():
            state.unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
