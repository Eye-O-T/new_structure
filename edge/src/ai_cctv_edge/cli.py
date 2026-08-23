from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import secrets
import shutil
import stat
import subprocess
import threading
import time
from pathlib import Path

from .config import (
    BackupConfig,
    ControlConfig,
    EdgeConfig,
    RecoveryConfig,
    RtspConfig,
    VideoConfig,
    render_toml,
    write_atomic,
)
from .control import create_control_app
from .doctor import run_checks
from .pairing import (
    DISCOVERY_PORT,
    PairingSession,
    advertise_until_stopped,
    create_pairing_app,
    load_pairing_key,
)
from .recovery import create_app
from .runner import EdgeRunner
from .state import ProfileSelectionStore, default_state_root

try:  # Edge deploys on Linux; keep validation helpers importable elsewhere.
    import pwd
except ImportError:  # pragma: no cover - Windows development hosts only
    pwd = None  # type: ignore[assignment]

DEFAULT_CONFIG = Path("/etc/ai-cctv-edge/config.toml")
CONFIGURED_MARKER_NAME = ".configured"
SYSTEMD_UNITS = (
    "ai-cctv-edge.service",
    "ai-cctv-edge-control.service",
    "ai-cctv-edge-recovery.service",
)


def _prompt(label: str, default: str) -> str:
    value = input(f"{label} [{default}]: ").strip()
    return value or default


def _publish_password_from_file(path: Path, camera_id: str) -> str:
    source = path.expanduser().resolve()
    if not source.is_file() or source.stat().st_size > 8192:
        raise ValueError("publish credentials file is missing or unexpectedly large")
    if os.name != "nt" and stat.S_IMODE(source.stat().st_mode) & 0o077:
        raise ValueError("publish credentials file must not be group/world accessible")
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("publish credentials file is not valid UTF-8 JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("publish credentials file must contain an object")
    if payload.get("camera_id") != camera_id or payload.get("username") != camera_id:
        raise ValueError("publish credentials do not match the configured camera ID")
    password = payload.get("password")
    if (
        not isinstance(password, str)
        or len(password) < 16
        or password != password.strip()
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in password)
    ):
        raise ValueError("publish credentials contain an invalid password")
    return password


def export_auth_token(config_path: Path, output_path: Path) -> int:
    """Copy the Edge bearer token to a new private handoff file without printing it."""

    config = EdgeConfig.load(config_path)
    source = config.control.token_file.expanduser().resolve()
    target = output_path.expanduser().resolve()
    if target == source:
        raise ValueError("auth token output must differ from the live token file")
    if target.exists():
        raise FileExistsError("auth token output already exists")
    try:
        if not source.is_file() or source.stat().st_size > 8192:
            raise ValueError("Edge auth token file is missing or unexpectedly large")
        raw_token = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError("Edge auth token cannot be read as UTF-8") from exc
    token = raw_token.rstrip("\r\n")
    if (
        len(token) < 32
        or token != token.strip()
        or raw_token not in {token, token + "\n", token + "\r\n"}
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in token)
    ):
        raise ValueError("Edge auth token is invalid")

    target.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(target, flags, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(target, 0o600)
        sudo_uid = os.environ.get("SUDO_UID")
        sudo_gid = os.environ.get("SUDO_GID")
        if os.name != "nt" and sudo_uid and sudo_gid:
            os.chown(target, int(sudo_uid), int(sudo_gid))
    except Exception:
        target.unlink(missing_ok=True)
        raise
    print(f"Edge auth token handoff written: {target}")
    print("Transfer it securely to the central Configurator, then remove this copy.")
    return 0


def setup(path: Path, publish_credentials_file: Path | None = None) -> int:
    device_id = _prompt("Device ID", "edge-001")
    camera_id = _prompt("Camera ID", "cam-001")
    mode = _prompt("RTSP mode (central_pull/central_publish)", "central_publish")
    central_host = _prompt("Central server address", "192.0.2.10")
    profile = _prompt("Video profile (hd/fhd)", "hd").lower()
    supported = tuple(
        item.strip().lower()
        for item in _prompt("Supported profiles (comma-separated)", "hd,fhd").split(",")
        if item.strip()
    )
    backup_root = Path(_prompt("Backup root", "/var/lib/ai-cctv-edge/recordings"))
    password_file = path.parent / "publish.password"
    recovery_token_file = path.parent / "recovery.token"
    config = EdgeConfig(
        schema_version=1,
        device_id=device_id,
        camera_id=camera_id,
        video=VideoConfig.from_profile(
            profile,
            supported_profiles=supported,
        ),
        rtsp=RtspConfig(
            mode=mode,
            central_host=central_host,
            username=camera_id,
            password_file=password_file,
        ),
        backup=BackupConfig(root=backup_root),
        recovery=RecoveryConfig(token_file=recovery_token_file),
        control=ControlConfig(token_file=recovery_token_file),
    )
    config.validate()

    # Validate and prepare every secret before replacing the live config. A
    # malformed Configurator handoff or an aborted password prompt must leave
    # the currently running configuration and profile selection untouched.
    publish_password: str | None = None
    if mode == "central_publish":
        publish_password = (
            _publish_password_from_file(publish_credentials_file, camera_id)
            if publish_credentials_file is not None
            else getpass.getpass(
                "Camera publish password generated by the server Configurator: "
            ).strip()
        )
        if (
            len(publish_password) < 16
            or any(
                ord(character) < 0x20 or ord(character) == 0x7F
                for character in publish_password
            )
        ):
            raise ValueError("publish password must contain at least 16 characters")
    elif not password_file.exists():
        publish_password = secrets.token_urlsafe(32)
    recovery_token = (
        None if recovery_token_file.exists() else secrets.token_urlsafe(48)
    )

    try:
        account = pwd.getpwnam("ai-cctv-edge") if pwd is not None else None
    except KeyError:
        account = None

    if publish_password is not None:
        write_atomic(password_file, publish_password + "\n", mode=0o600)
    if recovery_token is not None:
        write_atomic(recovery_token_file, recovery_token + "\n", mode=0o600)
    write_atomic(path, render_toml(config))
    if path == DEFAULT_CONFIG or "AI_CCTV_EDGE_STATE_ROOT" in os.environ:
        selection_store = ProfileSelectionStore()
        _previous, generation = selection_store.read(profile)
        selection_store.write(profile, generation + 1)
    if account is not None:
        for target in (path, password_file, recovery_token_file):
            if target.exists():
                shutil.chown(target, user=account.pw_uid, group=account.pw_gid)
    # This is the final persistent setup write. systemd units refuse to start
    # from the packaged example config until this marker exists.
    write_atomic(path.parent / CONFIGURED_MARKER_NAME, "configured\n", mode=0o644)
    print(f"Configuration written: {path}")
    print(f"Camera stream path: {camera_id}")
    print(
        f"Edge auth token file (copy securely to the central operator): {recovery_token_file}"
    )
    return 0


def systemctl(action: str) -> int:
    return subprocess.run(["systemctl", action, *SYSTEMD_UNITS], check=False).returncode


def pair(
    path: Path,
    *,
    device_id: str,
    camera_id: str,
    pairing_key_file: Path,
    bind_host: str,
    management_port: int,
    recovery_port: int,
    discovery_port: int,
    supported_profiles: tuple[str, ...],
    set_pairing_key: bool = False,
) -> int:
    """Advertise an unconfigured Edge and accept one authenticated setup."""

    session = PairingSession(
        config_path=path.expanduser().resolve(),
        pairing_key_file=pairing_key_file.expanduser().resolve(),
        device_id=device_id,
        camera_id=camera_id,
        management_port=management_port,
        recovery_port=recovery_port,
    )
    if session.marker_path.exists():
        raise ValueError("Edge is already configured; pairing mode is first-setup only")
    if set_pairing_key:
        entered = getpass.getpass("New Edge pairing key (minimum 32 characters): ")
        confirmed = getpass.getpass("Confirm Edge pairing key: ")
        if entered != confirmed:
            raise ValueError("pairing key confirmation does not match")
        if len(entered) < 32 or entered != entered.strip() or any(
            ord(character) < 0x20 or ord(character) == 0x7F
            for character in entered
        ):
            raise ValueError(
                "pairing key must contain at least 32 printable characters"
            )
        write_atomic(session.pairing_key_file, entered + "\n", mode=0o640)
    pairing_key = load_pairing_key(session.pairing_key_file)

    import uvicorn

    stopped = threading.Event()
    advertiser = threading.Thread(
        target=advertise_until_stopped,
        kwargs={
            "stop": stopped,
            "device_id": device_id,
            "camera_id": camera_id,
            "management_port": management_port,
            "recovery_port": recovery_port,
            "supported_profiles": supported_profiles,
            "pairing_key": pairing_key,
            "discovery_port": discovery_port,
        },
        name="edge-pairing-advertiser",
        daemon=True,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            create_pairing_app(session),
            host=bind_host,
            port=management_port,
            log_config=None,
        )
    )

    def stop_after_completion() -> None:
        session.completed.wait()
        if session.completed.is_set():
            time.sleep(0.25)
            server.should_exit = True

    watcher = threading.Thread(
        target=stop_after_completion,
        name="edge-pairing-completion",
        daemon=True,
    )
    advertiser.start()
    watcher.start()
    print(
        f"Pairing mode: device={device_id} camera={camera_id} "
        f"discovery=UDP/{discovery_port} management={bind_host}:{management_port}"
    )
    print("Open the central Configurator, enter the same Edge key, and scan the LAN.")
    try:
        server.run()
    finally:
        stopped.set()
        advertiser.join(timeout=2)
    if not session.completed.is_set():
        return 1
    print(f"Pairing complete: {session.config_path}")
    return 0


def show_status(path: Path) -> int:
    subprocess.run(
        ["systemctl", "--no-pager", "--full", "status", *SYSTEMD_UNITS],
        check=False,
    )
    state = default_state_root() / "status.json"
    if state.exists():
        print(state.read_text(encoding="utf-8"))
    else:
        print(json.dumps({"state": "unknown", "config": str(path)}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ai-cctv-edge")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in (
        "start",
        "stop",
        "restart",
        "status",
        "doctor",
        "logs",
        "run",
        "serve-control",
        "serve-recovery",
    ):
        sub.add_parser(command)
    for command in ("setup", "configure"):
        configure = sub.add_parser(command)
        configure.add_argument(
            "--publish-credentials-file",
            type=Path,
            help="Configurator JSON handoff file for this camera",
        )
    export_token = sub.add_parser(
        "export-auth-token",
        help="copy the Edge bearer token to a new mode-0600 handoff file",
    )
    export_token.add_argument("--output", type=Path, required=True)
    pairing = sub.add_parser(
        "pair",
        help="advertise this unconfigured Edge and accept central provisioning",
    )
    pairing.add_argument("--device-id", default="edge-001")
    pairing.add_argument("--camera-id", default="cam-001")
    pairing.add_argument(
        "--pairing-key-file",
        type=Path,
        default=DEFAULT_CONFIG.parent / "recovery.token",
    )
    pairing.add_argument("--bind-host", default="0.0.0.0")
    pairing.add_argument("--management-port", type=int, default=8003)
    pairing.add_argument("--recovery-port", type=int, default=8002)
    pairing.add_argument("--discovery-port", type=int, default=DISCOVERY_PORT)
    pairing.add_argument("--supported-profiles", default="hd,fhd")
    pairing.add_argument(
        "--set-pairing-key",
        action="store_true",
        help="replace the unconfigured Edge key from a hidden confirmation prompt",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s service=edge %(message)s",
    )
    if args.command in {"setup", "configure"}:
        result = setup(args.config, args.publish_credentials_file)
        if result == 0 and args.config == DEFAULT_CONFIG:
            systemctl("enable")
            return systemctl("restart")
        return result
    if args.command == "export-auth-token":
        return export_auth_token(args.config, args.output)
    if args.command == "pair":
        supported_profiles = tuple(
            item.strip().lower()
            for item in args.supported_profiles.split(",")
            if item.strip()
        )
        result = pair(
            args.config,
            device_id=args.device_id,
            camera_id=args.camera_id,
            pairing_key_file=args.pairing_key_file,
            bind_host=args.bind_host,
            management_port=args.management_port,
            recovery_port=args.recovery_port,
            discovery_port=args.discovery_port,
            supported_profiles=supported_profiles,
            set_pairing_key=args.set_pairing_key,
        )
        if result == 0 and args.config == DEFAULT_CONFIG:
            systemctl("enable")
            return systemctl("restart")
        return result
    if args.command in {"start", "stop", "restart"}:
        return systemctl(args.command)
    if args.command == "logs":
        return subprocess.run(
            [
                "journalctl",
                *[item for unit in SYSTEMD_UNITS for item in ("-u", unit)],
                "-f",
            ],
            check=False,
        ).returncode
    if args.command == "status":
        return show_status(args.config)
    if args.command == "doctor":
        config = EdgeConfig.load(args.config)
        checks = run_checks(config)
        for check in checks:
            print(f"[{check.status}] {check.name}: {check.message}")
        return 1 if any(check.status == "ERROR" for check in checks) else 0
    if args.command == "run":
        return EdgeRunner(args.config).run()
    if args.command == "serve-control":
        import uvicorn

        config = EdgeConfig.load(args.config)
        uvicorn.run(
            create_control_app(args.config),
            host=config.control.bind_host,
            port=config.control.port,
            log_config=None,
        )
        return 0
    if args.command == "serve-recovery":
        import uvicorn

        config = EdgeConfig.load(args.config)
        uvicorn.run(
            create_app(args.config),
            host=config.recovery.bind_host,
            port=config.recovery.port,
            log_config=None,
        )
        return 0
    parser.error("unsupported command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
