# Edge 설치 설정, Pairing, 상태 확인, 서비스 제어 명령을 연결하는 CLI 진입점이다.
# 인증값은 파일로 전달하고 실제 영상 실행과 HTTP 처리는 각 전용 모듈에 맡긴다.
from __future__ import annotations

import argparse
import getpass
import json
import logging
import os
import re
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
    NetworkConfig,
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
)
from .recovery import create_app
from .runner import EdgeRunner
from .state import ProfileSelectionStore, default_state_root
from .network import interface_mac, selected_interface_ipv4
from .network_policy import apply_pairing, apply_persistent, verify_persistent
from .repair import PACKAGE_TRANSITION, REPAIR_ROOT, repair_lock, repair_pairing

try:  # Windows에서도 설정 검증을 사용할 수 있게 한다.
    import pwd
except ImportError:  # pragma: no cover - Windows 개발 환경용
    pwd = None  # type: ignore[assignment]

DEFAULT_CONFIG = Path("/etc/ai-cctv-edge/config.toml")
CONFIGURED_MARKER_NAME = ".configured"
SYSTEMD_UNITS = (
    "ai-cctv-edge-network.service",
    "ai-cctv-edge.service",
    "ai-cctv-edge-control.service",
    "ai-cctv-edge-recovery.service",
)


def _prompt(label: str, default: str) -> str:
    value = input(f"{label} [{default}]: ").strip()
    return value or default


# 인계 JSON의 접근 권한·대상 카메라·비밀번호 형식을 검증한 뒤 비밀번호만 반환한다.
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
    """Edge 토큰을 출력하지 않고 권한을 제한한 전달 파일에 복사한다."""

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
    print("Transfer it securely to the central server install helper, then remove this copy.")
    return 0


# 대화형 입력과 전달 파일을 검증한 뒤 설정·인증 파일·설치 완료 표식을 순서대로 기록한다.
def setup(
    path: Path,
    publish_credentials_file: Path | None = None,
    interface: str | None = None,
) -> int:
    selected = interface or _prompt("Network interface", "")
    if not selected:
        raise ValueError("NETWORK_INTERFACE_REQUIRED: select the Edge–Server interface")
    selected_interface_ipv4(selected)
    if pwd is None or os.geteuid() != 0:
        raise PermissionError("setup requires root on Linux to install the network policy")
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
        network=NetworkConfig(interface=selected),
    )
    config.validate()

    # 잘못된 전달 파일이나 입력 취소로 기존 설정이 바뀌지 않도록 인증값부터 검증한다.
    publish_password: str | None = None
    if mode == "central_publish":
        publish_password = (
            _publish_password_from_file(publish_credentials_file, camera_id)
            if publish_credentials_file is not None
            else getpass.getpass(
                "Camera publish password generated by the server server install helper: "
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
    apply_persistent(config)
    write_atomic(path, render_toml(config))
    if path == DEFAULT_CONFIG or "AI_CCTV_EDGE_STATE_ROOT" in os.environ:
        selection_store = ProfileSelectionStore()
        _previous, generation = selection_store.read(profile)
        selection_store.write(profile, generation + 1)
    if account is not None:
        for target in (path, password_file, recovery_token_file):
            if target.exists():
                shutil.chown(target, user=account.pw_uid, group=account.pw_gid)
    # 이 완료 표시를 마지막에 기록해야 systemd가 예제 설정으로 서비스를 시작하지 않는다.
    write_atomic(path.parent / CONFIGURED_MARKER_NAME, "configured\n", mode=0o644)
    print(f"Configuration written: {path}")
    print(f"Camera stream path: {camera_id}")
    print(
        f"Edge auth token file (copy securely to the central operator): {recovery_token_file}"
    )
    return 0


# 캡처·관리·복구 서비스를 하나의 명령으로 제어하고 systemctl의 종료 코드를 전달한다.
def systemctl(action: str) -> int:
    return subprocess.run(["systemctl", action, *SYSTEMD_UNITS], check=False).returncode


def serve_http_on_interface(app, config, *, recovery: bool) -> None:
    import uvicorn

    interface = config.network.interface
    address = selected_interface_ipv4(interface).address
    settings = config.recovery if recovery else config.control
    if settings.bind_host not in {"0.0.0.0", address}:
        raise ValueError("HTTP bind_host must be 0.0.0.0 or the selected interface IPv4")
    server = uvicorn.Server(
        uvicorn.Config(app, host=address, port=settings.port, log_config=None)
    )
    changed = threading.Event()
    stopped = threading.Event()

    def watch_address() -> None:
        while not stopped.wait(1):
            try:
                current = selected_interface_ipv4(interface).address
            except ValueError:
                current = None
            if current != address:
                changed.set()
                server.should_exit = True
                return

    watcher = threading.Thread(target=watch_address, name="edge-http-address-watch", daemon=True)
    watcher.start()
    try:
        server.run()
    finally:
        stopped.set()
        watcher.join(timeout=2)
    if changed.is_set():
        raise RuntimeError("selected interface IPv4 changed; restarting HTTP service")


def _run_pairing(
    path: Path,
    *,
    device_id: str,
    camera_id: str,
    token_file: Path | None = None,
    bind_host: str,
    management_port: int,
    recovery_port: int,
    discovery_port: int,
    supported_profiles: tuple[str, ...],
    interface: str | None = None,
    existing_config: EdgeConfig | None = None,
    repair_timeout: float | None = None,
) -> int:
    """미설정 Edge를 광고하고 인증된 초기 설정을 한 번 수락한다."""

    if not interface:
        raise ValueError("an IPv4 interface is required for pairing")
    if pwd is None or os.geteuid() != 0:
        raise PermissionError("pairing requires root on Linux to install the network policy")

    session = PairingSession(
        config_path=path.expanduser().resolve(),
        token_file=(token_file or path.parent / "recovery.token").expanduser().resolve(),
        device_id=device_id,
        camera_id=camera_id,
        interface=interface,
        existing_config=existing_config,
        management_port=management_port,
        recovery_port=recovery_port,
    )
    if session.marker_path.exists() and existing_config is None:
        raise ValueError("Edge is already configured; pairing mode is first-setup only")
    # Fail before starting uvicorn or the advertiser if interface details are invalid.
    selected_network = selected_interface_ipv4(interface)
    if bind_host not in {"0.0.0.0", selected_network.address}:
        raise ValueError("pairing bind host must be 0.0.0.0 or the selected interface IPv4")
    interface_mac(interface)
    apply_pairing(interface, selected_network.address, management_port)
    import uvicorn

    stopped = threading.Event()
    advertiser_failures: list[Exception] = []

    def run_advertiser() -> None:
        while not server.started and not server.should_exit:
            if stopped.wait(0.05):
                return
        if not server.started:
            return
        try:
            advertise_until_stopped(
                stop=stopped,
                device_id=device_id,
                camera_id=camera_id,
                management_port=management_port,
                recovery_port=recovery_port,
                supported_profiles=supported_profiles,
                discovery_port=discovery_port,
                interface=interface,
                failures=advertiser_failures,
            )
        except Exception:
            server.should_exit = True

    advertiser = threading.Thread(
        target=run_advertiser,
        name="edge-pairing-advertiser",
        daemon=True,
    )
    server = uvicorn.Server(
        uvicorn.Config(
            create_pairing_app(session),
            host=(
                selected_network.address
                if bind_host == "0.0.0.0"
                else bind_host
            ),
            port=management_port,
            log_config=None,
        )
    )

    completion_watch_stop = threading.Event()
    timed_out = threading.Event()

    # HTTP 핸들러가 완료를 표시하면 Uvicorn이 해당 응답을 마친 다음 종료한다.
    def stop_after_completion() -> None:
        deadline = None if repair_timeout is None else time.monotonic() + repair_timeout
        while not completion_watch_stop.wait(0.1):
            if session.completed.is_set():
                server.should_exit = True
                return
            if deadline is not None and time.monotonic() >= deadline:
                timed_out.set()
                server.should_exit = True
                return

    watcher = threading.Thread(
        target=stop_after_completion,
        name="edge-pairing-completion",
        daemon=True,
    )
    advertiser.start()
    watcher.start()
    print(
        f"Pairing mode: device={device_id} camera={camera_id} "
        f"discovery=UDP/{discovery_port} management={selected_network.address}:{management_port}"
    )
    print("Open the central server Install Helper and scan the same LAN for this Edge.")
    if repair_timeout is not None:
        print(f"Repair pairing expires after {int(repair_timeout)} seconds.")
    try:
        server.run()
    finally:
        stopped.set()
        advertiser.join(timeout=2)
        completion_watch_stop.set()
        watcher.join(timeout=1)
    if not session.completed.is_set():
        if advertiser_failures:
            raise RuntimeError("pairing discovery stopped because the selected interface failed") from advertiser_failures[0]
        if timed_out.is_set():
            raise TimeoutError("Repair pairing timed out")
        return 1
    print(f"Pairing complete: {session.config_path}")
    return 0


def pair(*args, **kwargs) -> int:
    with repair_lock():
        if PACKAGE_TRANSITION.exists():
            raise RuntimeError("Edge package upgrade is in progress")
        return _run_pairing(*args, **kwargs)


# systemd 서비스 상태와 마지막 캡처 스냅샷을 함께 출력해 제어·영상 문제를 구분하게 한다.
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


# 서브커맨드별 인수를 검증하고 설치·진단·캡처·관리·복구 실행을 각 담당 모듈에 위임한다.
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
        "apply-network-policy",
        "remove-network-policy",
        "verify-network-policy",
    ):
        sub.add_parser(command)
    sub.choices["doctor"].add_argument(
        "--interface",
        help="inspect the Edge Ethernet interface and route to the central server",
    )
    for command in ("setup", "configure"):
        configure = sub.add_parser(command)
        configure.add_argument(
            "--publish-credentials-file",
            type=Path,
            help="server install helper JSON handoff file for this camera",
        )
        configure.add_argument("--interface", help="Linux interface used for all Edge networking")
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
    pairing.add_argument("--bind-host", default="0.0.0.0")
    pairing.add_argument("--management-port", type=int, default=8003)
    pairing.add_argument("--recovery-port", type=int, default=8002)
    pairing.add_argument("--discovery-port", type=int, default=DISCOVERY_PORT)
    pairing.add_argument("--supported-profiles", default="hd,fhd")
    pairing.add_argument(
        "--interface",
        required=True,
        help="IPv4 interface used for directed discovery broadcast, e.g. eth0",
    )
    repair = sub.add_parser("repair", help="re-pair a configured Edge while preserving local state")
    repair.add_argument("--device-id")
    repair.add_argument("--camera-id")
    repair.add_argument("--interface")
    recover_repair = sub.add_parser("recover-repair", help=argparse.SUPPRESS)
    recover_repair.add_argument("--defer-services", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s service=edge %(message)s",
    )
    if args.command in {"setup", "configure"}:
        with repair_lock():
            if PACKAGE_TRANSITION.exists():
                raise RuntimeError("Edge package upgrade is in progress")
            result = setup(args.config, args.publish_credentials_file, args.interface)
        if result == 0 and args.config == DEFAULT_CONFIG:
            if PACKAGE_TRANSITION.exists():
                raise RuntimeError("Edge package upgrade is in progress")
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
            bind_host=args.bind_host,
            management_port=args.management_port,
            recovery_port=args.recovery_port,
            discovery_port=args.discovery_port,
            supported_profiles=supported_profiles,
            interface=args.interface,
        )
        if result == 0 and args.config == DEFAULT_CONFIG:
            if PACKAGE_TRANSITION.exists():
                raise RuntimeError("Edge package upgrade is in progress")
            systemctl("enable")
            return systemctl("restart")
        return result
    if args.command == "repair":
        if args.config != DEFAULT_CONFIG:
            parser.error("repair requires the standard system configuration")
        try:
            config = EdgeConfig.load(args.config)
            device_id = config.device_id if args.device_id is None else args.device_id
            camera_id = config.camera_id if args.camera_id is None else args.camera_id
            interface = config.network.interface if args.interface is None else args.interface
            if not interface:
                raise ValueError("NETWORK_INTERFACE_REQUIRED: specify --interface")
            if not device_id or len(device_id) > 128 or any(ord(c) < 32 or ord(c) == 127 for c in device_id):
                raise ValueError("invalid device_id")
            if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", camera_id):
                raise ValueError("invalid camera_id")

            def run_repair_pairing(old, selected_device, selected_camera, selected_interface):
                return _run_pairing(
                    args.config,
                    device_id=selected_device,
                    camera_id=selected_camera,
                    bind_host="0.0.0.0",
                    management_port=old.control.port,
                    recovery_port=old.recovery.port,
                    discovery_port=DISCOVERY_PORT,
                    supported_profiles=old.video.supported_profiles,
                    interface=selected_interface,
                    existing_config=old,
                    repair_timeout=600,
                )

            return repair_pairing(
                args.config,
                device_id=device_id,
                camera_id=camera_id,
                interface=interface,
                run_pairing=run_repair_pairing,
            )
        except KeyboardInterrupt:
            print("Repair interrupted; restoring the previous local configuration.")
            try:
                from .repair import recover_pending

                recover_pending(args.config)
            except Exception as exc:
                print(f"Repair recovery is incomplete: {exc}")
                return 1
            return 130
        except Exception as exc:
            print(f"Repair failed: {exc}")
            return 1
    if args.command in {"start", "stop", "restart"}:
        if PACKAGE_TRANSITION.exists() and args.command != "stop":
            raise RuntimeError("Edge package upgrade is in progress")
        if args.command in {"start", "restart"} and (REPAIR_ROOT / "journal.json").exists():
            raise RuntimeError("Repair recovery must finish before Edge services can start")
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
        checks = run_checks(config, args.interface)
        try:
            verify_persistent(config)
        except (OSError, RuntimeError, ValueError) as exc:
            from .doctor import Check

            checks.append(Check("edge-network-policy", "ERROR", str(exc)))
        for check in checks:
            print(f"[{check.status}] {check.name}: {check.message}")
        return 1 if any(check.status == "ERROR" for check in checks) else 0
    if args.command == "run":
        if (REPAIR_ROOT / "journal.json").exists():
            raise RuntimeError("Repair recovery must finish before Edge services can start")
        return EdgeRunner(args.config).run()
    if args.command == "recover-repair":
        from .repair import recover_pending

        recover_pending(args.config, start_services=not args.defer_services)
        return 0
    if args.command == "apply-network-policy":
        if os.name != "posix" or os.geteuid() != 0:
            raise PermissionError("network policy command requires root")
        apply_persistent(EdgeConfig.load(args.config))
        return 0
    if args.command == "remove-network-policy":
        from .network_policy import remove_policy

        remove_policy()
        return 0
    if args.command == "verify-network-policy":
        if os.name != "posix" or os.geteuid() != 0:
            raise PermissionError("network policy verification requires root")
        config = EdgeConfig.load(args.config)
        verify_persistent(config)
        return 0
    if args.command == "serve-control":
        if (REPAIR_ROOT / "journal.json").exists():
            raise RuntimeError("Repair recovery must finish before Edge services can start")
        config = EdgeConfig.load(args.config)
        verify_persistent(config)
        serve_http_on_interface(create_control_app(args.config), config, recovery=False)
        return 0
    if args.command == "serve-recovery":
        if (REPAIR_ROOT / "journal.json").exists():
            raise RuntimeError("Repair recovery must finish before Edge services can start")
        config = EdgeConfig.load(args.config)
        verify_persistent(config)
        serve_http_on_interface(create_app(args.config), config, recovery=True)
        return 0
    parser.error("unsupported command")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
