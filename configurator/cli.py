from __future__ import annotations

import argparse
import getpass
from pathlib import Path

from ai_cctv_core.config import CameraBootstrap, load_config

from .compose_adapter import ComposeAdapter, default_server_dir
from .config_core import InstallRequest, initialize
from .doctor import checks
from .model_manager import install_from_manifest, validate_custom_model


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
            rtsp_bind_address=args.rtsp_bind,
            rtsp_port=args.rtsp_port,
        )
    )
    print(f"Configuration: {result.config_path}")
    print(f"Secrets: {result.secrets_path}")
    print(f"Camera credentials: {result.camera_credentials_path}")
    print(f"Compose environment: {result.compose_env_path}")
    if result.camera_credentials:
        print("Camera publish credentials (store each once on its Edge device):")
        for camera_id, credential in result.camera_credentials.items():
            print(f"  {camera_id}: {credential['username']} / {credential['password']}")
    return 0


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
        "--rtsp-bind",
        default="0.0.0.0",
        help="central LAN address to accept Edge RTSP publishers (default: all interfaces)",
    )
    init.add_argument("--rtsp-port", type=int, default=8554)

    validate = sub.add_parser("validate")
    validate.add_argument("config", type=Path)
    doctor = sub.add_parser("doctor")
    doctor.add_argument("config", type=Path)
    for command in ("start", "stop", "restart", "status", "logs"):
        sub.add_parser(command)
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
