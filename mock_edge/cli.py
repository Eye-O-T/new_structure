from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import threading
from pathlib import Path

import uvicorn

from .app import MockEdgeService, create_control_app, create_recovery_app
from .protocol import advertise_until_stopped, load_secret
from .runtime import MockEdgeRuntimeError, resolve_ffmpeg


LOGGER = logging.getLogger("ai_cctv.mock_edge")


def _port(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 65_535:
        raise argparse.ArgumentTypeError("port must be in range 1..65535")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _segment_seconds(value: str) -> int:
    parsed = int(value)
    if not 1 <= parsed <= 3600:
        raise argparse.ArgumentTypeError("segment seconds must be in range 1..3600")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m mock_edge",
        description=(
            "Loop an MP4 into central MediaMTX and emulate the AI_CCTV Edge APIs."
        ),
    )
    parser.add_argument("--video", required=True, type=Path, help="source MP4 path")
    parser.add_argument("--device-id", default="mock-edge-001")
    parser.add_argument("--camera-id", default="cam-001")
    parser.add_argument(
        "--pairing-key-file",
        required=True,
        type=Path,
        help="UTF-8 file containing the shared 32+ character pairing/bearer key",
    )
    parser.add_argument(
        "--state-dir", type=Path, default=Path(".mock-edge/state")
    )
    parser.add_argument(
        "--backup-dir", type=Path, default=Path(".mock-edge/recordings")
    )
    parser.add_argument("--ffmpeg", default="ffmpeg")
    parser.add_argument("--segment-seconds", type=_segment_seconds, default=10)
    parser.add_argument("--management-bind", default="0.0.0.0")
    parser.add_argument("--management-port", type=_port, default=8003)
    parser.add_argument("--recovery-bind", default="0.0.0.0")
    parser.add_argument("--recovery-port", type=_port, default=8002)
    parser.add_argument("--discovery-destination", default="255.255.255.255")
    parser.add_argument("--discovery-port", type=_port, default=37020)
    parser.add_argument("--discovery-interval", type=_positive_float, default=1.0)
    parser.add_argument(
        "--no-discovery",
        action="store_true",
        help="disable UDP discovery (use with direct configuration)",
    )
    parser.add_argument(
        "--central-host",
        help="direct mode: reachable MediaMTX host, for example 127.0.0.1",
    )
    parser.add_argument("--central-port", type=_port, default=8554)
    parser.add_argument(
        "--publish-credentials-file",
        type=Path,
        help="direct mode: protected camera_id/username/password JSON handoff",
    )
    parser.add_argument(
        "--profile", choices=("hd", "fhd"), default="hd", help="direct mode profile"
    )
    parser.add_argument("--log-level", choices=("debug", "info", "warning"), default="info")
    return parser


def _load_publish_credentials(path: Path, camera_id: str) -> tuple[str, str]:
    try:
        payload = json.loads(path.expanduser().resolve().read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("publish credentials file is not readable UTF-8 JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "camera_id",
        "username",
        "password",
    }:
        raise ValueError("publish credentials must contain camera_id, username and password")
    if payload["camera_id"] != camera_id or payload["username"] != camera_id:
        raise ValueError("publish credential identity does not match --camera-id")
    password = payload["password"]
    if not isinstance(password, str) or len(password) < 16:
        raise ValueError("publish credential password must contain at least 16 characters")
    return payload["username"], password


def _serve(server: uvicorn.Server, name: str) -> None:
    try:
        server.run()
    except Exception:
        LOGGER.exception("%s HTTP server stopped unexpectedly", name)


def run(args: argparse.Namespace) -> int:
    if args.management_port == args.recovery_port:
        raise ValueError("management and recovery ports must differ")
    direct_values = (args.central_host, args.publish_credentials_file)
    if any(direct_values) and not all(direct_values):
        raise ValueError(
            "direct mode requires both --central-host and --publish-credentials-file"
        )
    video = args.video.expanduser().resolve(strict=True)
    if not video.is_file():
        raise ValueError("--video must identify a regular file")
    pairing_key = load_secret(args.pairing_key_file, name="pairing key", minimum=32)
    ffmpeg = resolve_ffmpeg(args.ffmpeg)
    service = MockEdgeService(
        device_id=args.device_id,
        camera_id=args.camera_id,
        pairing_key=pairing_key,
        state_root=args.state_dir.expanduser(),
        backup_root=args.backup_dir.expanduser(),
        video_path=video,
        ffmpeg=ffmpeg,
        segment_seconds=args.segment_seconds,
    )
    if args.central_host and args.publish_credentials_file:
        if service.configured:
            raise ValueError(
                "Mock Edge is already configured; remove its state directory to re-pair"
            )
        username, password = _load_publish_credentials(
            args.publish_credentials_file, args.camera_id
        )
        service.configure_direct(
            central_host=args.central_host,
            central_port=args.central_port,
            username=username,
            password=password,
            profile=args.profile,
        )

    control_server = uvicorn.Server(
        uvicorn.Config(
            create_control_app(service),
            host=args.management_bind,
            port=args.management_port,
            log_level=args.log_level,
        )
    )
    recovery_server = uvicorn.Server(
        uvicorn.Config(
            create_recovery_app(service),
            host=args.recovery_bind,
            port=args.recovery_port,
            log_level=args.log_level,
        )
    )
    servers = (
        (control_server, "management"),
        (recovery_server, "recovery"),
    )
    server_threads = [
        threading.Thread(
            target=_serve,
            args=(server, name),
            name=f"mock-edge-{name}",
            daemon=True,
        )
        for server, name in servers
    ]
    discovery_stop = threading.Event()
    discovery_thread: threading.Thread | None = None
    if not args.no_discovery and not service.configured:
        discovery_thread = threading.Thread(
            target=advertise_until_stopped,
            kwargs={
                "stop": discovery_stop,
                "device_id": args.device_id,
                "camera_id": args.camera_id,
                "management_port": args.management_port,
                "recovery_port": args.recovery_port,
                "supported_profiles": ("hd", "fhd"),
                "pairing_key": pairing_key,
                "discovery_port": args.discovery_port,
                "interval_seconds": args.discovery_interval,
                "destination": args.discovery_destination,
            },
            name="mock-edge-discovery",
            daemon=True,
        )
    stop = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    previous_sigint = signal.signal(signal.SIGINT, request_stop)
    previous_sigterm = signal.signal(signal.SIGTERM, request_stop)
    try:
        service.start()
        for thread in server_threads:
            thread.start()
        if discovery_thread is not None:
            discovery_thread.start()
        LOGGER.info(
            "Mock Edge ready: management=http://%s:%d recovery=http://%s:%d",
            args.management_bind,
            args.management_port,
            args.recovery_bind,
            args.recovery_port,
        )
        if not service.configured:
            LOGGER.info("waiting for Configurator pairing")
        while not stop.wait(0.5):
            if service.configured:
                discovery_stop.set()
            if any(not thread.is_alive() for thread in server_threads):
                LOGGER.error("an HTTP server exited")
                return 1
        return 0
    finally:
        discovery_stop.set()
        control_server.should_exit = True
        recovery_server.should_exit = True
        service.stop()
        if discovery_thread is not None:
            discovery_thread.join(timeout=2)
        for thread in server_threads:
            thread.join(timeout=5)
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        return run(args)
    except (MockEdgeRuntimeError, OSError, ValueError) as exc:
        parser.error(str(exc))
    return 2


if __name__ == "__main__":
    sys.exit(main())
