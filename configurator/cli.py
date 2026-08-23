from __future__ import annotations

import argparse
import getpass
import json
from pathlib import Path
from typing import Any

from ai_cctv_core.config import CameraBootstrap, load_config

from .compose_adapter import ComposeAdapter, default_server_dir
from .config_core import InstallRequest, initialize
from .doctor import checks
from .model_manager import install_from_manifest, validate_custom_model
from .server_api import (
    ServerApiClient,
    ServerApiError,
    prepare_private_output,
    redact_for_display,
    write_publish_credentials,
)


def _camera(value: str) -> CameraBootstrap:
    camera_id, separator, name = value.partition(":")
    if not separator:
        name = camera_id
    return CameraBootstrap(camera_id=camera_id, name=name)


def _init(args: argparse.Namespace) -> int:
    password = args.admin_password or getpass.getpass("Administrator password: ")
    if args.model_manifest is not None:
        model = install_from_manifest(
            args.model_manifest.resolve(), args.data_root.resolve() / "model-downloads"
        )
    else:
        model = args.model.resolve()
        validate_custom_model(model)
    result = initialize(
        InstallRequest(
            data_root=args.data_root,
            server_dir=args.server_dir,
            admin_username=args.admin_username,
            admin_password=password,
            model_path=model,
            cameras=args.camera,
            public_http_port=args.http_port,
            public_https_port=args.https_port,
            public_bind_address=args.public_bind,
            public_base_url=args.public_base_url,
            rtsp_bind_address=args.rtsp_bind,
            rtsp_port=args.rtsp_port,
        )
    )
    print(f"Configuration: {result.config_path}")
    print(f"Data service secrets: {result.secrets_path}")
    print(f"External service secrets: {result.external_secrets_path}")
    print(f"Inference service secrets: {result.inference_secrets_path}")
    print(f"Media service secrets: {result.media_secrets_path}")
    print(f"Camera credentials: {result.camera_credentials_path}")
    print(f"Compose environment: {result.compose_env_path}")
    if result.camera_credentials:
        print(
            "Camera publish credentials were written to the protected credentials "
            "file and are not printed."
        )
    return 0


def _secret_from_file(path: Path, label: str) -> str:
    if path.stat().st_size > 8192:
        raise ValueError(f"{label} file is unexpectedly large")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"{label} file is empty")
    return value


def _api_client(args: argparse.Namespace) -> ServerApiClient:
    password = (
        _secret_from_file(args.password_file, "administrator password")
        if args.password_file is not None
        else getpass.getpass("Server administrator password: ")
    )
    client = ServerApiClient(args.server_url)
    client.login(args.username, password)
    return client


def _print_api_result(result: dict[str, Any]) -> None:
    print(json.dumps(redact_for_display(result), ensure_ascii=False, indent=2))


def _api_command(args: argparse.Namespace) -> int:
    handoff_path: Path | None = None
    try:
        if args.command in {"edge-register", "edge-rotate-credentials"}:
            handoff_path = prepare_private_output(args.publish_credentials_output)
        client = _api_client(args)
        if args.command == "edge-register":
            edge_token = (
                _secret_from_file(args.edge_auth_token_file, "Edge auth token")
                if args.edge_auth_token_file is not None
                else getpass.getpass("Edge control API bearer token: ")
            )
            result = client.register_edge(
                camera_id=args.camera_id,
                name=args.name,
                edge_device_id=args.edge_device_id,
                edge_management_url=args.management_url,
                edge_recovery_url=args.recovery_url,
                edge_auth_token=edge_token,
            )
            write_publish_credentials(result, args.camera_id, handoff_path)
        elif args.command == "edge-update":
            edge_token = (
                _secret_from_file(args.edge_auth_token_file, "Edge auth token")
                if args.edge_auth_token_file is not None
                else None
            )
            result = client.update_edge(
                args.camera_id,
                edge_device_id=args.edge_device_id,
                edge_management_url=args.management_url,
                edge_recovery_url=args.recovery_url,
                edge_auth_token=edge_token,
            )
        elif args.command == "edge-rotate-credentials":
            result = client.rotate_publish_credentials(args.camera_id)
            write_publish_credentials(result, args.camera_id, handoff_path)
        elif args.command == "camera-status":
            result = client.camera_status(args.camera_id)
        elif args.command == "video-profile":
            result = client.video_profile(args.camera_id)
        elif args.command == "set-video-profile":
            result = client.set_video_profile(args.camera_id, args.profile)
        else:  # pragma: no cover - parser constrains this branch
            return 2
    except ServerApiError as exc:
        status = f" HTTP {exc.status_code}" if exc.status_code is not None else ""
        print(f"[ERROR] {exc.code}{status}: {exc.message}")
        return 1
    except (OSError, ValueError) as exc:
        print(f"[ERROR] CONFIGURATOR_INPUT: {exc}")
        return 1
    if handoff_path is not None:
        print(f"Publish credentials saved to: {handoff_path}")
    _print_api_result(result)
    return 0


def _add_api_auth(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--server-url", required=True, help="Nginx HTTPS base URL")
    parser.add_argument("--username", default="admin")
    parser.add_argument(
        "--password-file",
        type=Path,
        help="read the administrator password from a protected UTF-8 file",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-cctv-server")
    parser.add_argument("--server-dir", type=Path, default=default_server_dir())
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("--data-root", type=Path, required=True)
    init.add_argument("--admin-username", default="admin")
    init.add_argument("--admin-password", help=argparse.SUPPRESS)
    model = init.add_mutually_exclusive_group(required=True)
    model.add_argument("--model", type=Path)
    model.add_argument("--model-manifest", type=Path)
    init.add_argument("--camera", action="append", type=_camera, default=[])
    init.add_argument("--http-port", type=int, default=80)
    init.add_argument("--https-port", type=int, default=443)
    init.add_argument("--public-bind", default="127.0.0.1")
    init.add_argument(
        "--public-base-url",
        required=True,
        help="public HTTPS origin returned in Live and Playback URLs",
    )
    init.add_argument(
        "--rtsp-bind",
        default="127.0.0.1",
        help=(
            "RTSP listener address (default: loopback); explicitly provide the "
            "central server's trusted-LAN IP to accept remote Edge publishers"
        ),
    )
    init.add_argument("--rtsp-port", type=int, default=8554)

    validate = sub.add_parser("validate")
    validate.add_argument("config", type=Path)
    doctor = sub.add_parser("doctor")
    doctor.add_argument("config", type=Path)
    for command in ("start", "stop", "restart", "status", "logs"):
        sub.add_parser(command)

    edge_register = sub.add_parser("edge-register")
    _add_api_auth(edge_register)
    edge_register.add_argument("camera_id")
    edge_register.add_argument("--name", required=True)
    edge_register.add_argument("--edge-device-id", required=True)
    edge_register.add_argument("--management-url", required=True)
    edge_register.add_argument("--recovery-url", required=True)
    edge_register.add_argument(
        "--publish-credentials-output",
        type=Path,
        required=True,
        help="atomically save the one-time RTSP credential to this private file",
    )
    edge_register.add_argument(
        "--edge-auth-token-file",
        type=Path,
        help="read the Edge bearer token from a protected UTF-8 file",
    )

    edge_update = sub.add_parser("edge-update")
    _add_api_auth(edge_update)
    edge_update.add_argument("camera_id")
    edge_update.add_argument("--edge-device-id")
    edge_update.add_argument("--management-url")
    edge_update.add_argument("--recovery-url")
    edge_update.add_argument("--edge-auth-token-file", type=Path)

    rotate_credentials = sub.add_parser("edge-rotate-credentials")
    _add_api_auth(rotate_credentials)
    rotate_credentials.add_argument("camera_id")
    rotate_credentials.add_argument(
        "--publish-credentials-output",
        type=Path,
        required=True,
        help="atomically save the rotated RTSP credential to this private file",
    )

    camera_status = sub.add_parser("camera-status")
    _add_api_auth(camera_status)
    camera_status.add_argument("camera_id")

    profile = sub.add_parser("video-profile")
    _add_api_auth(profile)
    profile.add_argument("camera_id")

    set_profile = sub.add_parser("set-video-profile")
    _add_api_auth(set_profile)
    set_profile.add_argument("camera_id")
    set_profile.add_argument("profile", choices=("hd", "fhd"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init":
        return _init(args)
    if args.command == "validate":
        config = load_config(args.config)
        print(f"valid schema_version={config.schema_version}")
        return 0
    if args.command == "doctor":
        results = checks(args.server_dir, args.config)
        for result in results:
            print(f"[{result.status}] {result.name}: {result.message}")
        return 1 if any(result.status == "ERROR" for result in results) else 0

    if args.command in {
        "edge-register",
        "edge-update",
        "edge-rotate-credentials",
        "camera-status",
        "video-profile",
        "set-video-profile",
    }:
        return _api_command(args)

    adapter = ComposeAdapter(args.server_dir)
    if args.command == "start":
        return adapter.start()
    if args.command == "stop":
        return adapter.stop()
    if args.command == "restart":
        return adapter.restart()
    if args.command == "status":
        return adapter.run("ps").returncode
    if args.command == "logs":
        return adapter.run("logs", "--tail=200", "-f").returncode
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
