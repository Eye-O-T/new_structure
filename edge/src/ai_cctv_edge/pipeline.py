from __future__ import annotations

import shlex
from pathlib import Path

from .config import EdgeConfig


def daily_backup_directory(config: EdgeConfig, compact_utc_date: str) -> Path:
    return (
        config.backup.root
        / config.camera_id
        / compact_utc_date[0:4]
        / compact_utc_date[4:6]
        / compact_utc_date[6:8]
    )


def _encoder_command(config: EdgeConfig) -> list[str]:
    if config.video.encoder == "x264enc":
        return [
            "x264enc",
            "tune=zerolatency",
            "speed-preset=ultrafast",
            f"bitrate={config.video.bitrate_kbps}",
            f"key-int-max={config.video.fps}",
            "bframes=0",
        ]
    if config.video.encoder == "v4l2h264enc":
        # video_bitrate is the standard V4L2 MPEG control exposed through the
        # plugin. Device-specific register addresses are deliberately avoided.
        return [
            "v4l2h264enc",
            f"extra-controls=controls,video_bitrate={config.video.bitrate_kbps * 1000}",
        ]
    raise ValueError("video.encoder must be x264enc or v4l2h264enc")


def build_gstreamer_command(
    config: EdgeConfig, compact_utc_timestamp: str
) -> list[str]:
    day = compact_utc_timestamp[:8]
    backup_dir = daily_backup_directory(config, day)
    backup_dir.mkdir(parents=True, exist_ok=True)
    location = backup_dir / f"{compact_utc_timestamp}_%06d.ts"
    nanoseconds = config.backup.segment_seconds * 1_000_000_000

    command = [
        "gst-launch-1.0",
        "-e",
        "libcamerasrc",
        "!",
        (
            "video/x-raw,"
            f"width={config.video.width},height={config.video.height},"
            f"framerate={config.video.fps}/1"
        ),
        "!",
        "watchdog",
        f"timeout={int(config.monitoring.frame_timeout_seconds * 1000)}",
        "!",
        "videoconvert",
        "!",
    ]
    command.extend(_encoder_command(config))

    command.extend(
        [
            "!",
            "h264parse",
            "config-interval=1",
            "!",
            "tee",
            "name=t",
            "t.",
            "!",
            "queue",
            "max-size-time=0",
            "max-size-bytes=0",
            "!",
            "splitmuxsink",
            f"location={location}",
            "muxer-factory=mpegtsmux",
            f"max-size-time={nanoseconds}",
            "async-handling=true",
            "t.",
            "!",
            "queue",
            "leaky=downstream",
            "max-size-buffers=60",
            "!",
        ]
    )

    if config.rtsp.mode == "central_pull":
        command.extend(
            [
                "flvmux",
                "streamable=true",
                "!",
                "rtmpsink",
                f"location=rtmp://127.0.0.1:1935/{config.camera_id}",
            ]
        )
    else:
        command.extend(
            [
                "shmsink",
                f"socket-path=/run/ai-cctv-edge/{config.camera_id}.h264.sock",
                "wait-for-connection=false",
                "sync=false",
                "shm-size=16777216",
            ]
        )
    return command


def build_profile_probe_command(config: EdgeConfig) -> list[str]:
    """Build a bounded encoder preflight that does not seize the real camera."""

    command = [
        "gst-launch-1.0",
        "-q",
        "videotestsrc",
        f"num-buffers={config.video.fps * 2}",
        "!",
        (
            "video/x-raw,"
            f"width={config.video.width},height={config.video.height},"
            f"framerate={config.video.fps}/1"
        ),
        "!",
        "videoconvert",
        "!",
    ]
    command.extend(_encoder_command(config))
    command.extend(["!", "h264parse", "!", "fakesink", "sync=false"])
    return command


def redacted_command(command: list[str]) -> str:
    redacted = []
    for part in command:
        if part.startswith("location=rtsp://") and "@" in part:
            prefix, host = part.split("@", maxsplit=1)
            user = prefix.split("://", maxsplit=1)[1].split(":", maxsplit=1)[0]
            part = f"location=rtsp://{user}:***@{host}"
        redacted.append(shlex.quote(part))
    return " ".join(redacted)


def render_edge_mediamtx_config(config: EdgeConfig) -> str:
    if config.rtsp.mode != "central_pull":
        raise ValueError("edge MediaMTX config is only used in central_pull mode")
    return f"""logLevel: info
rtspAddress: :{config.rtsp.edge_port}
rtmpAddress: 127.0.0.1:1935
hls: no
webrtc: no
srt: no
api: yes
apiAddress: 127.0.0.1:9997
authMethod: internal
authInternalUsers:
  - user: any
    ips: [127.0.0.1, ::1]
    permissions:
      - action: publish
        path: {config.camera_id}
      - action: api
  - user: any
    permissions:
      - action: read
        path: {config.camera_id}
pathDefaults:
  source: publisher
  record: no
paths:
  {config.camera_id}:
"""
