from __future__ import annotations

import shutil
import socket
import subprocess
import tempfile
from dataclasses import dataclass

from .config import EdgeConfig


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    message: str


def _command(name: str) -> Check:
    path = shutil.which(name)
    return Check(name, "OK" if path else "ERROR", path or "command not found")


def _plugin(name: str) -> Check:
    try:
        result = subprocess.run(
            ["gst-inspect-1.0", name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=10,
            check=False,
        )
        return Check(
            f"GStreamer {name}",
            "OK" if result.returncode == 0 else "ERROR",
            "available" if result.returncode == 0 else "plugin not available",
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return Check(f"GStreamer {name}", "ERROR", type(exc).__name__)


def run_checks(config: EdgeConfig) -> list[Check]:
    checks = [_command("gst-launch-1.0"), _command("gst-inspect-1.0")]
    for plugin in (
        "libcamerasrc",
        "watchdog",
        "videoconvert",
        config.video.encoder,
        "h264parse",
        "splitmuxsink",
        "mpegtsmux",
        "videotestsrc",
        "fakesink",
    ):
        checks.append(_plugin(plugin))
    if config.rtsp.mode == "central_pull":
        checks.append(_plugin("rtmpsink"))
    else:
        for plugin in ("shmsink", "shmsrc", "rtspclientsink"):
            checks.append(_plugin(plugin))

    if config.rtsp.mode == "central_pull":
        checks.append(
            Check(
                "MediaMTX binary",
                "OK" if config.rtsp.mediamtx_binary.is_file() else "ERROR",
                str(config.rtsp.mediamtx_binary),
            )
        )

    camera_tool = shutil.which("rpicam-hello") or shutil.which("libcamera-hello")
    if camera_tool:
        result = subprocess.run(
            [camera_tool, "--list-cameras"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        camera_ok = result.returncode == 0 and "Available cameras" in result.stdout
        checks.append(
            Check(
                "Camera",
                "OK" if camera_ok else "ERROR",
                "enumerated" if camera_ok else "not found",
            )
        )
    else:
        checks.append(Check("Camera", "ERROR", "rpicam-hello not found"))

    try:
        camera_root = config.backup.root / config.camera_id
        camera_root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=camera_root):
            pass
        usage = shutil.disk_usage(camera_root)
        checks.append(Check("Backup storage", "OK", f"free_bytes={usage.free}"))
    except OSError as exc:
        checks.append(Check("Backup storage", "ERROR", str(exc)))

    network_name = (
        "Central RTSP" if config.rtsp.mode == "central_publish" else "Edge RTSP"
    )
    network_host = (
        config.rtsp.central_host
        if config.rtsp.mode == "central_publish"
        else "127.0.0.1"
    )
    network_port = (
        config.rtsp.central_port
        if config.rtsp.mode == "central_publish"
        else config.rtsp.edge_port
    )
    try:
        with socket.create_connection((network_host, network_port), timeout=3):
            pass
        checks.append(Check(network_name, "OK", "TCP connection succeeded"))
    except OSError as exc:
        checks.append(Check(network_name, "WARN", str(exc)))

    secret_paths = [("Edge auth token", config.control.token_file)]
    if config.recovery.token_file != config.control.token_file:
        secret_paths.append(("Recovery token alias", config.recovery.token_file))
    if config.rtsp.mode == "central_publish":
        secret_paths.append(("Publish password", config.rtsp.password_file))
    for name, path in secret_paths:
        exists = path.is_file()
        checks.append(Check(name, "OK" if exists else "ERROR", str(path)))
    return checks
