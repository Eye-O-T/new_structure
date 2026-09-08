# GStreamer 명령을 구성한다. 카메라 영상을 H.264로 압축한 뒤 저장·송출 경로로 나눈다.
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
        # 플러그인이 제공하는 표준 V4L2 제어값을 써서 장치별 레지스터 주소에 의존하지 않는다.
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

    # tee는 하나의 압축 영상을 두 갈래로 전달한다. 각 queue가 처리 속도 차이를 흡수한다.
    # 송출 쪽은 밀린 프레임을 버려(leaky) 네트워크 지연이 로컬 녹화를 막지 않게 한다.
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
        # pull 방식은 Edge의 MediaMTX를 중앙이 읽고, publish 방식은 중앙으로 직접 보낸다.
        # publish의 공유 메모리(shmsink)는 별도 송출 프로세스와 영상을 나누는 통로다.
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
    """실제 카메라를 점유하지 않는 유한 길이의 인코더 시험 명령을 만든다."""

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
