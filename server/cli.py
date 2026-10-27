# 터미널 명령을 파싱해 설정 생성, Compose 실행, 중앙 관리자 API 호출로 연결한다.
# 비밀번호는 보호 파일이나 숨김 입력으로 받고 조회 결과는 비밀값을 제거해 출력한다.

from __future__ import annotations

import argparse
import os
import getpass
import json
import sys
from pathlib import Path
from typing import Any

from ai_cctv_core.config import CameraBootstrap, load_config

from server.operations.compose import (
    ComposeAdapter,
    default_data_root,
    default_server_dir,
    installation_prerequisites,
)
from server.deployment.signature import (
    ContractSignatureMismatchError,
    reset_incompatible_deployment,
)
from server.installer.configuration import InstallRequest, initialize
from server.installer.models import validate_custom_model
from server.installer.workflow import _validate_install_input, inspect_installation, install_new
from server.desktop.launcher import (
    admin_arguments,
    launch_desktop,
    server_manager_arguments,
)
from server.operations.doctor import checks
from server.operations.local_camera import (
    list_camera_devices,
    load_device_setting,
    publisher_status,
    start_publisher,
    stop_publisher,
)
from server.operations.services import run_service_action
from server.edge_onboarding.host_helper import run_host_helper
from server.edge_onboarding.server_api import (
    ServerApiClient,
    ServerApiError,
    prepare_private_output,
    redact_for_display,
    write_publish_credentials,
)


# camera_id:name 입력을 부트스트랩 카메라로 바꾸며 이름을 생략하면 ID를 표시 이름으로 사용한다.
def _camera(value: str) -> CameraBootstrap:
    camera_id, separator, name = value.partition(":")
    if not separator:
        name = camera_id
    return CameraBootstrap(camera_id=camera_id, name=name)


def _launch_manager(args: argparse.Namespace) -> int:
    try:
        process = launch_desktop(
            "server_manager", server_manager_arguments(args.server_dir, args.data_root)
        )
    except (OSError, ValueError) as exc:
        print(f"[ERROR] DESKTOP_LAUNCH_FAILED: {exc}")
        return 1
    print(f"서버 제어판 실행됨 (PID {process.pid}).")
    return 0


def _prompt_reconfiguration(data_root: Path, server_dir: Path, notice: str) -> None:
    """계약 불일치로 초기화된 뒤 GUI 설치 마법사 또는 대화형 CLI로 새 설정값을 입력받는다."""

    if os.getenv("AI_CCTV_NO_GUI_ON_RESET") != "1":
        try:
            from PyQt5.QtWidgets import QApplication
            from server.installer.gui import InstallerWindow

            app = QApplication.instance() or QApplication(sys.argv[:1])
            window = InstallerWindow(
                server_dir=server_dir,
                data_root=data_root,
                reset_notice=notice,
            )
            window.show()
            app.exec_()
            return
        except Exception:
            pass

    if not sys.stdin.isatty():
        return

    cert_dir = data_root.expanduser().resolve() / "certs"
    cert_path = cert_dir / "tls.crt" if (cert_dir / "tls.crt").is_file() else None
    key_path = cert_dir / "tls.key" if (cert_dir / "tls.key").is_file() else None

    while True:
        try:
            print("\n--- 새로운 서버 기본 설정 입력 ---")
            username = input("관리자 계정 [admin]: ").strip() or "admin"
            password = getpass.getpass("관리자 비밀번호 (4~12자): ").strip()
            scheme = (
                input("통신 방식 (https/http) [http]: ").strip() or "http"
            ).lower()
            default_url = f"{scheme}://127.0.0.1"
            base_url = input(f"접속 주소 [{default_url}]: ").strip() or default_url
        except (EOFError, KeyboardInterrupt):
            return
        request = InstallRequest(
            data_root=data_root,
            server_dir=server_dir,
            admin_username=username,
            admin_password=password,
            model_path=None,
            cameras=[],
            public_scheme=scheme,
            allow_insecure_http=(scheme == "http"),
            public_base_url=base_url,
            tls_certificate_path=cert_path if scheme == "https" else None,
            tls_private_key_path=key_path if scheme == "https" else None,
        )
        try:
            install_new(request, check_environment=False, require_tls=(scheme == "https"))
        except ValueError as exc:
            print(f"[ERROR] {exc}")
            continue
        print("[OK] 새로운 설정값이 저장되었습니다.")
        return


def _inspect_or_reset_installation(data_root: Path, server_dir: Path):
    """설치 정보를 확인하고 계약 해시 불일치 시 초기화 후 새 값 입력을 유도한다."""

    try:
        return inspect_installation(data_root, server_dir)
    except ContractSignatureMismatchError as exc:
        print(f"[WARN] CONTRACT_SIGNATURE_MISMATCH:\n{exc.user_message}")
        reset_incompatible_deployment(data_root, server_dir)
        print("[OK] 기존 저장 데이터가 초기화되었습니다 (models/, certs/ 보존).")
        _prompt_reconfiguration(data_root, server_dir, exc.user_message)
        return inspect_installation(data_root, server_dir)


def _launch_admin(args: argparse.Namespace) -> int:
    try:
        deployment = _inspect_or_reset_installation(args.data_root, args.server_dir)
        if deployment is None:
            raise ValueError("no installed deployment was found; run the installer first")
        process = launch_desktop("admin", admin_arguments(deployment))
    except (OSError, ValueError) as exc:
        print(f"[ERROR] DESKTOP_LAUNCH_FAILED: {exc}")
        return 1
    print(f"CCTV 관제 화면 실행됨 (PID {process.pid}).")
    return 0


def _installed_deployment(args: argparse.Namespace):
    deployment = _inspect_or_reset_installation(args.data_root, args.server_dir)
    if deployment is None:
        raise ValueError("no installed deployment was found; run the installer first")
    return deployment


def _managed_service_command(args: argparse.Namespace, action: str) -> int:
    """Use the same guarded service operation as Server Manager."""

    try:
        deployment = _installed_deployment(args)
        message = run_service_action(args.server_dir, deployment, action, print)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"[ERROR] SERVICE_ACTION_FAILED: {exc}")
        return 1
    print(message)
    return 0


def _local_camera_command(args: argparse.Namespace) -> int:
    if args.local_camera_action == "devices":
        print(json.dumps({"devices": list_camera_devices()}, ensure_ascii=False))
        return 0
    if args.local_camera_action == "status":
        try:
            status = publisher_status(args.data_root)
        except OSError as exc:
            print(f"[ERROR] LOCAL_CAMERA_FAILED: {exc}")
            return 1
        print(json.dumps(status, ensure_ascii=False))
        return 0
    if args.local_camera_action == "off":
        try:
            stopped = stop_publisher(args.data_root)
        except OSError as exc:
            print(f"[ERROR] LOCAL_CAMERA_FAILED: {exc}")
            return 1
        print("Local camera publisher stopped." if stopped else "Local camera publisher is not running.")
        return 0
    try:
        current = publisher_status(args.data_root)
        if current["status"] == "running":
            requested = args.device or load_device_setting(args.data_root)
            if requested and requested != current.get("device_name"):
                raise RuntimeError(
                    "a different local camera is already publishing; stop it first"
                )
            print(json.dumps(current, ensure_ascii=False))
            return 0
        deployment = _installed_deployment(args)
        device = args.device or load_device_setting(args.data_root)
        if not device:
            raise ValueError(
                "a camera device must be selected with --device on the first start"
            )
        password = (
            _secret_from_file(args.password_file, "administrator password")
            if args.password_file is not None
            else getpass.getpass("Server administrator password: ")
        )
        # Import lazily so ordinary CLI service commands do not load desktop dependencies.
        from server.desktop.admin.api import DesktopApi
        from server.desktop.admin.local_camera import rtsp_publish_url

        certificate = deployment.data_root / "certs" / "tls.crt"
        api = DesktopApi(
            deployment.public_url,
            ca_file=certificate if certificate.is_file() else None,
            allow_insecure_http=deployment.allow_insecure_http,
        )
        api.login(args.username, password)
        try:
            try:
                result = api.register_local_camera("local-camera", device)
            except ServerApiError as exc:
                if exc.status_code != 409:
                    raise
                api.camera(
                    "local-camera",
                    method="PATCH",
                    payload={"name": device, "enabled": True},
                )
                result = api.camera(
                    "local-camera", "/publish-credentials/rotate", "POST"
                )
            credentials = result.get("publish_credentials") or {}
            url = rtsp_publish_url(
                deployment.rtsp_host,
                deployment.rtsp_port,
                "local-camera",
                credentials["username"],
                credentials["password"],
            )
            status = start_publisher(
                args.data_root,
                device_name=device,
                camera_id="local-camera",
                stream_url=url,
            )
        finally:
            api.logout()
    except (KeyError, OSError, ValueError, RuntimeError, ServerApiError) as exc:
        print(f"[ERROR] LOCAL_CAMERA_FAILED: {exc}")
        return 1
    print(json.dumps(status, ensure_ascii=False))
    return 0


def _edge_helper_command(args: argparse.Namespace) -> int:
    try:
        payload = None
        if args.edge_action in {"probe", "pair"}:
            if args.input is not None:
                if args.input.stat().st_size > 1024 * 1024:
                    raise ValueError("Host Helper input is too large")
                raw = args.input.read_text(encoding="utf-8")
            else:
                raw = sys.stdin.read(1024 * 1024 + 1)
                if len(raw) > 1024 * 1024:
                    raise ValueError("Host Helper input is too large")
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError("Host Helper input must be a JSON object")
        result = run_host_helper(args.edge_action, payload, timeout=args.timeout)
    except (OSError, UnicodeError, ValueError, RuntimeError, json.JSONDecodeError) as exc:
        print(f"[ERROR] HOST_HELPER_FAILED: {exc}")
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


# 공통 초기화 요청을 구성하고 생성된 파일 위치만 안내한다. 서비스 실행은 별도 단계가 맡는다.
def _init(args: argparse.Namespace) -> int:
    try:
        if args.admin_password_file is not None:
            password = _secret_from_file(
                args.admin_password_file, "administrator password"
            )
        else:
            password = args.admin_password or getpass.getpass(
                "Administrator password: "
            )
        model = args.model.resolve() if args.model else None
        if model is not None:
            validate_custom_model(model)
        compose_env_path = (
            args.compose_env.expanduser().resolve()
            if args.compose_env is not None
            else args.data_root.expanduser().resolve() / "config" / "compose.env"
        )
        request = InstallRequest(
            data_root=args.data_root,
            server_dir=args.server_dir,
            admin_username=args.admin_username,
            admin_password=password,
            model_path=model,
            identity_model_path=args.identity_model,
            cameras=args.camera,
            compose_env_path=compose_env_path,
            tls_certificate_path=args.tls_certificate,
            tls_private_key_path=args.tls_private_key,
            public_http_port=args.http_port,
            public_https_port=args.https_port,
            public_bind_address=args.public_bind,
            public_base_url=args.public_base_url or f"{args.public_scheme}://127.0.0.1",
            public_scheme=args.public_scheme,
            allow_insecure_http=args.allow_insecure_http,
            rtsp_bind_address=args.rtsp_bind,
            rtsp_port=args.rtsp_port,
            recording_segment_seconds=args.recording_segment_seconds,
            retention_days=args.retention_days,
            storage_warning_free_percent=args.storage_warning_free_percent,
            inference_device=args.inference_device,
        )
        if getattr(args, "reset_existing", False):
            # 명시적 재설정만 기존 파일을 백업하고 인증키를 교체한다.
            _validate_install_input(request, require_tls=False)
            result = initialize(request)
        else:
            result = install_new(request, check_environment=False, require_tls=False)
    except (EOFError, OSError, ValueError, RuntimeError) as exc:
        print(f"[ERROR] INITIALIZATION_FAILED: {exc}")
        print(
            "No service was started. Preserve existing files; use start for an "
            "initialized deployment, or diagnose an interrupted installation."
        )
        return 1
    print(f"Configuration: {result.config_path}")
    print(f"Data service secrets: {result.secrets_path}")
    print(f"External service secrets: {result.external_secrets_path}")
    print(f"Preprocessing service secrets: {result.preprocessing_secrets_path}")
    print(f"Analysis service secrets: {result.analysis_secrets_path}")
    print(f"Media service secrets: {result.media_secrets_path}")
    print(f"Camera credentials: {result.camera_credentials_path}")
    print(f"Release manifest: {result.release_manifest_path}")
    print(f"Compose environment: {result.compose_env_path}")
    if model is not None:
        print(f"Installed model: {args.data_root.resolve() / 'models' / model.name}")
    if result.tls_certificate_path.is_file():
        print(f"TLS certificate: {result.tls_certificate_path}")
        print(f"TLS private key: {result.tls_private_key_path}")
    elif args.public_scheme == "https":
        print(
            "[WARN] TLS certificate/key are not installed. Provide --tls-certificate "
            "and --tls-private-key before starting services."
        )
    if result.camera_credentials:
        print(
            "Camera publish credentials were written to the protected credentials "
            "file and are not printed."
        )
    return 0


# Docker와 서버 패키지 검사 결과를 모두 출력하고 하나라도 실패하면 비정상 종료 코드를 돌려준다.
def _preflight(server_dir: Path) -> int:
    results = installation_prerequisites(server_dir)
    for result in results:
        print(f"[{'OK' if result.ok else 'ERROR'}] {result.name}: {result.message}")
    return 0 if all(item.ok for item in results) else 1


# 기동에 필요한 파일·도구 중 실패 항목만 표시해 start 호출 전에 중단 여부를 결정한다.
def _print_failed_prerequisites(adapter: ComposeAdapter) -> bool:
    failed = [item for item in adapter.deployment_prerequisites() if not item.ok]
    for item in failed:
        print(f"[ERROR] {item.name}: {item.message}")
    return bool(failed)


# 사전 점검과 설정 생성을 통과한 뒤에만 서비스를 시작한다.
def _install(args: argparse.Namespace) -> int:
    if _preflight(args.server_dir) != 0:
        print("[ERROR] INSTALLATION_BLOCKED: satisfy the prerequisites and retry.")
        return 1
    certificate = args.data_root.expanduser().resolve() / "certs" / "tls.crt"
    private_key = args.data_root.expanduser().resolve() / "certs" / "tls.key"
    supplied_pair = (
        args.tls_certificate is not None and args.tls_private_key is not None
    )
    https_selected = args.public_scheme == "https"
    if https_selected and not supplied_pair and not (certificate.is_file() and private_key.is_file()):
        print(
            "[ERROR] TLS_REQUIRED: install requires --tls-certificate and "
            "--tls-private-key (or an existing pair in the data root)."
        )
        return 1
    if _init(args) != 0:
        return 1
    env_file = (
        args.compose_env
        if args.compose_env is not None
        else args.data_root.expanduser().resolve() / "config" / "compose.env"
    )
    try:
        adapter = ComposeAdapter(args.server_dir, env_file)
        if _print_failed_prerequisites(adapter):
            print("[ERROR] SERVICE_START_BLOCKED: correct the files above and retry.")
            return 1
        return_code = adapter.start()
    except OSError as exc:
        print(f"[ERROR] SERVICE_START_FAILED: {exc}")
        return 1
    if return_code != 0:
        print(
            "[ERROR] SERVICE_START_FAILED: Docker Compose returned "
            f"exit code {return_code}. Run doctor for details."
        )
        return return_code
    print("[OK] AI CCTV services are running.")
    return 0


# 비밀값 입력 파일의 크기와 빈 내용을 확인하고 값 자체는 오류 메시지에 포함하지 않는다.
def _secret_from_file(path: Path, label: str) -> str:
    if path.stat().st_size > 8192:
        raise ValueError(f"{label} file is unexpectedly large")
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError(f"{label} file is empty")
    return value


# 보호 파일 또는 숨김 입력으로 받은 관리자 암호로 로그인한 클라이언트를 만든다.
def _api_client(args: argparse.Namespace) -> ServerApiClient:
    password = (
        _secret_from_file(args.password_file, "administrator password")
        if args.password_file is not None
        else getpass.getpass("Server administrator password: ")
    )
    client = ServerApiClient(args.server_url)
    client.login(args.username, password)
    return client


# 중첩된 토큰·비밀번호를 가린 결과만 JSON으로 출력한다.
def _print_api_result(result: dict[str, Any]) -> None:
    print(json.dumps(redact_for_display(result), ensure_ascii=False, indent=2))


# 최초 Edge 등록 후 게시 계정을 보호 파일로 인계한다.
def _api_command(args: argparse.Namespace) -> int:
    try:
        handoff_path = prepare_private_output(args.publish_credentials_output)
        client = _api_client(args)
        edge_token = (
            _secret_from_file(args.edge_auth_token_file, "Edge auth token")
            if args.edge_auth_token_file is not None
            else getpass.getpass("Edge control API bearer token: ")
        )
        result = client.register_edge(
            camera_id=args.camera_id,
            name=args.name,
            edge_device_id=args.edge_device_id,
            edge_mac_address=args.edge_mac_address,
            edge_management_url=args.management_url,
            edge_recovery_url=args.recovery_url,
            edge_auth_token=edge_token,
        )
        write_publish_credentials(result, args.camera_id, handoff_path)
    except ServerApiError as exc:
        status = f" HTTP {exc.status_code}" if exc.status_code is not None else ""
        print(f"[ERROR] {exc.code}{status}: {exc.message}")
        return 1
    except (OSError, ValueError) as exc:
        print(f"[ERROR] INSTALL_HELPER_INPUT: {exc}")
        return 1
    print(f"Publish credentials saved to: {handoff_path}")
    _print_api_result(result)
    return 0


# 관리자 API 명령에서 사용할 서버 주소·계정·암호 파일 인수를 공통으로 정의한다.
def _add_api_auth(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--server-url", required=True, help="Nginx HTTPS base URL")
    parser.add_argument("--username", default="admin")
    parser.add_argument(
        "--password-file",
        type=Path,
        help="read the administrator password from a protected UTF-8 file",
    )


# init과 install이 동일한 모델·TLS·녹화 기본값과 입력 규칙을 공유하게 한다.
def _add_initialization_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--reset-existing",
        action="store_true",
        help=(
            "explicitly replace existing configuration and rotate all JWT/service "
            "keys after making .bak files; does not reset existing DB user passwords"
        ),
    )
    parser.add_argument(
        "--data-root",
        type=Path,
        default=default_data_root(),
        help="persistent data directory (default: platform ProgramData directory)",
    )
    parser.add_argument("--compose-env", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--admin-username", default="admin")
    password = parser.add_mutually_exclusive_group()
    password.add_argument("--admin-password", help=argparse.SUPPRESS)
    password.add_argument(
        "--admin-password-file",
        type=Path,
        help="read the administrator password from a protected UTF-8 file",
    )
    parser.add_argument(
        "--model",
        type=Path,
        help="optional path to an already-downloaded .pt, .onnx, or .engine model",
    )
    parser.add_argument(
        "--identity-model",
        type=Path,
        help="prepared OSNet ONNX; otherwise look in persistent/package models directories",
    )
    parser.add_argument(
        "--inference-device",
        default="auto",
        help="inference device: auto, cpu, cuda, or cuda:<index> (default: auto)",
    )
    tls = parser.add_argument_group("TLS files")
    tls.add_argument(
        "--tls-certificate",
        type=Path,
        help="local PEM certificate copied to protected persistent storage",
    )
    tls.add_argument(
        "--tls-private-key",
        type=Path,
        help="matching local PEM private key; must be supplied with the certificate",
    )
    parser.add_argument("--camera", action="append", type=_camera, default=[])
    parser.add_argument("--http-port", type=int, default=80)
    parser.add_argument("--https-port", type=int, default=443)
    parser.add_argument(
        "--public-scheme",
        choices=("http", "https"),
        default="https",
        help="public transport scheme; HTTPS requires a certificate/key pair",
    )
    parser.add_argument(
        "--allow-insecure-http",
        action="store_true",
        help="explicitly allow HTTP mode without TLS (required with --public-scheme http)",
    )
    parser.add_argument("--public-bind", default="127.0.0.1")
    parser.add_argument(
        "--public-base-url",
        default=None,
        help="public HTTP/HTTPS origin returned in Live and Playback URLs",
    )
    parser.add_argument(
        "--rtsp-bind",
        default="127.0.0.1",
        help=(
            "RTSP listener address (default: loopback); explicitly provide the "
            "central server's trusted-LAN IP to accept remote Edge publishers"
        ),
    )
    parser.add_argument("--rtsp-port", type=int, default=8554)
    recording = parser.add_argument_group("recording and retention")
    recording.add_argument(
        "--recording-segment-seconds",
        type=int,
        choices=range(10, 301),
        default=60,
        metavar="10..300",
        help="central MediaMTX recording segment length (default: 60)",
    )
    recording.add_argument(
        "--retention-days",
        type=int,
        default=7,
        help="central recording retention in days (default: 7)",
    )
    recording.add_argument(
        "--storage-warning-free-percent",
        type=int,
        choices=range(1, 100),
        default=15,
        metavar="1..99",
        help="warn when free recording storage falls below this percent",
    )


# 설치·진단·서비스 제어·첫 Edge 등록 명령을 구성하고 서비스별 env 선택도 허용한다.
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-cctv-server")
    parser.add_argument("--server-dir", type=Path, default=default_server_dir())
    parser.add_argument(
        "--env-file",
        type=Path,
        help="Compose environment file (accepted before or after service commands)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser(
        "init",
        help="create configuration, scoped secrets, and Compose environment",
        description=(
            "Create persistent deployment files. On Windows, run this command "
            "from an Administrator terminal when using the default ProgramData path."
        ),
    )
    _add_initialization_arguments(init)
    install = sub.add_parser(
        "install",
        help="check prerequisites, initialize, and start all services",
        description=(
            "Perform first-time setup and start services. On Windows, run from "
            "an Administrator terminal."
        ),
    )
    _add_initialization_arguments(install)
    sub.add_parser("preflight", help="check Docker and installed server package")

    validate = sub.add_parser("validate")
    validate.add_argument("config", type=Path)
    doctor = sub.add_parser("doctor")
    doctor.add_argument("config", type=Path)
    doctor.add_argument("--env-file", type=Path, default=argparse.SUPPRESS)
    for command in ("start", "stop", "restart", "status", "logs"):
        service = sub.add_parser(command)
        service.add_argument("--env-file", type=Path, default=argparse.SUPPRESS)
        service.add_argument("--data-root", type=Path, default=default_data_root())

    for service_name in ("preprocessing", "analysis"):
        service = sub.add_parser(service_name)
        service.add_argument("state", choices=("on", "off"))
        service.add_argument("--data-root", type=Path, default=default_data_root())

    local_camera = sub.add_parser("local-camera")
    local_camera.add_argument(
        "local_camera_action", choices=("on", "off", "status", "devices")
    )
    local_camera.add_argument("--data-root", type=Path, default=default_data_root())
    local_camera.add_argument("--device", help="exact DirectShow camera device name")
    local_camera.add_argument("--username", default="admin")
    local_camera.add_argument("--password-file", type=Path)

    edge = sub.add_parser("edge")
    edge_sub = edge.add_subparsers(dest="edge_action", required=True)
    edge_discover = edge_sub.add_parser("discover")
    edge_discover.add_argument("--timeout", type=float, default=3.0)
    for action, default_timeout in (("probe", 5.0), ("pair", 10.0)):
        edge_action = edge_sub.add_parser(action)
        edge_action.add_argument(
            "--input", type=Path, help="JSON input file; defaults to standard input"
        )
        edge_action.add_argument("--timeout", type=float, default=default_timeout)

    manager = sub.add_parser("manager", help="서버 제어판 실행")
    manager.add_argument("--data-root", type=Path, default=default_data_root())
    admin = sub.add_parser("admin", help="CCTV 관제 화면 실행")
    admin.add_argument("--data-root", type=Path, default=default_data_root())

    edge_register = sub.add_parser("edge-register")
    _add_api_auth(edge_register)
    edge_register.add_argument("camera_id")
    edge_register.add_argument("--name", required=True)
    edge_register.add_argument("--edge-device-id", required=True)
    edge_register.add_argument("--edge-mac-address", required=True)
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

    return parser


# 파싱한 명령을 공통 기능으로 분기하고 파일·Docker 오류를 CLI 종료 코드로 전달한다.
def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "init":
        return _init(args)
    if args.command == "install":
        return _install(args)
    if args.command == "preflight":
        return _preflight(args.server_dir)
    if args.command == "validate":
        try:
            config = load_config(args.config)
        except (OSError, ValueError) as exc:
            print(f"[ERROR] CONFIGURATION_INVALID: {exc}")
            return 1
        else:
            print(f"valid schema_version={config.schema_version}")
            return 0
    if args.command == "doctor":
        results = checks(args.server_dir, args.config, env_file=args.env_file)
        for result in results:
            print(f"[{result.status}] {result.name}: {result.message}")
        return 1 if any(result.status == "ERROR" for result in results) else 0

    if args.command == "edge-register":
        return _api_command(args)
    if args.command == "manager":
        return _launch_manager(args)
    if args.command == "admin":
        return _launch_admin(args)
    if args.command in {"preprocessing", "analysis"}:
        return _managed_service_command(args, f"{args.command}-{args.state}")
    if args.command == "local-camera":
        return _local_camera_command(args)
    if args.command == "edge":
        return _edge_helper_command(args)

    if args.command in {"start", "stop", "restart", "status"}:
        try:
            if args.command == "stop":
                stop_publisher(args.data_root)
            deployment = _inspect_or_reset_installation(args.data_root, args.server_dir)
        except (OSError, ValueError) as exc:
            print(f"[ERROR] SERVICE_ACTION_FAILED: {exc}")
            return 1
        # An explicit env file preserves the legacy raw Compose CLI contract.
        if deployment is not None and args.env_file is None:
            return _managed_service_command(args, args.command)

    adapter = ComposeAdapter(args.server_dir, args.env_file)
    try:
        if args.command == "start":
            if _print_failed_prerequisites(adapter):
                print(
                    "[ERROR] SERVICE_START_BLOCKED: initialize the deployment or "
                    "correct the files above."
                )
                return 1
            return adapter.start()
        if args.command == "stop":
            return adapter.stop()
        if args.command == "restart":
            return adapter.restart()
        if args.command == "status":
            return adapter.run("ps").returncode
        if args.command == "logs":
            return adapter.run("logs", "--tail=200", "-f").returncode
    except OSError as exc:
        print(f"[ERROR] DOCKER_COMMAND_FAILED: {exc}")
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
