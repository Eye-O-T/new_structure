"""Read, validate, and safely update network settings of an existing install."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit
from collections.abc import Callable

import yaml

from server.deployment.validation import read_deployment_env
from server.deployment.context import DeploymentContext
from server.installer.configuration import (
    _dotenv,
    _validate_public_base_url,
    _validate_tls_files,
)
from server.operations.compose import ComposeAdapter


NETWORK_KEYS = (
    "PUBLIC_SCHEME", "PUBLIC_BIND_ADDRESS", "PUBLIC_BASE_URL",
    "PUBLIC_HTTP_PORT", "PUBLIC_HTTPS_PORT", "RTSP_BIND_ADDRESS", "RTSP_PORT",
    "ALLOW_INSECURE_HTTP", "COOKIE_SECURE", "NGINX_CONFIG_FILE",
)


@dataclass(frozen=True)
class NetworkSettings:
    public_scheme: str
    public_base_url: str
    public_bind_address: str
    public_http_port: int
    public_https_port: int
    rtsp_bind_address: str
    rtsp_port: int
    allow_insecure_http: bool
    cookie_secure: bool
    nginx_config_file: Path


def _bool(value: str, key: str) -> bool:
    normalized = value.strip().lower()
    if normalized not in {"true", "false"}:
        raise ValueError(f"{key} must be true or false")
    return normalized == "true"


def _int(value: str, key: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be an integer") from exc
    if not 1 <= result <= 65535:
        raise ValueError(f"{key} must be between 1 and 65535")
    return result


def load_network_settings(env_file: Path, server_dir: Path) -> NetworkSettings:
    values = read_deployment_env(env_file)
    scheme = values.get("PUBLIC_SCHEME", "https").lower()
    base_url = values.get("PUBLIC_BASE_URL", "")
    public_url = _validate_public_base_url(base_url)
    return NetworkSettings(
        public_scheme=scheme,
        public_base_url=public_url,
        public_bind_address=values.get("PUBLIC_BIND_ADDRESS", "127.0.0.1"),
        public_http_port=_int(values.get("PUBLIC_HTTP_PORT", "80"), "PUBLIC_HTTP_PORT"),
        public_https_port=_int(values.get("PUBLIC_HTTPS_PORT", "443"), "PUBLIC_HTTPS_PORT"),
        rtsp_bind_address=values.get("RTSP_BIND_ADDRESS", "127.0.0.1"),
        rtsp_port=_int(values.get("RTSP_PORT", "8554"), "RTSP_PORT"),
        allow_insecure_http=_bool(values.get("ALLOW_INSECURE_HTTP", "false"), "ALLOW_INSECURE_HTTP"),
        cookie_secure=_bool(values.get("COOKIE_SECURE", "true"), "COOKIE_SECURE"),
        nginx_config_file=Path(values.get("NGINX_CONFIG_FILE", server_dir / "services" / "nginx" / "nginx.conf")),
    )


def validate_network_settings(settings: NetworkSettings, *, cert_path: Path | None = None, key_path: Path | None = None) -> None:
    if settings.public_scheme not in {"http", "https"}:
        raise ValueError("PUBLIC_SCHEME must be http or https")
    if settings.public_scheme == "http" and not settings.allow_insecure_http:
        raise ValueError("HTTP requires ALLOW_INSECURE_HTTP=true")
    if settings.public_scheme == "https" and settings.allow_insecure_http:
        raise ValueError("HTTPS requires ALLOW_INSECURE_HTTP=false")
    if settings.cookie_secure != (settings.public_scheme == "https"):
        raise ValueError("COOKIE_SECURE must match PUBLIC_SCHEME")
    for value, key in ((settings.public_bind_address, "PUBLIC_BIND_ADDRESS"), (settings.rtsp_bind_address, "RTSP_BIND_ADDRESS")):
        try:
            ip_address(value)
        except ValueError as exc:
            raise ValueError(f"{key} must be a valid IP address") from exc
    if len({settings.public_http_port, settings.public_https_port, settings.rtsp_port}) != 3:
        raise ValueError("HTTP, HTTPS, and RTSP ports must be distinct")
    if settings.public_base_url:
        parsed = urlsplit(settings.public_base_url)
        if parsed.scheme != settings.public_scheme:
            raise ValueError("PUBLIC_BASE_URL scheme must match PUBLIC_SCHEME")
        expected = settings.public_http_port if settings.public_scheme == "http" else settings.public_https_port
        if (parsed.port or (80 if settings.public_scheme == "http" else 443)) != expected:
            raise ValueError("PUBLIC_BASE_URL port must match the selected web port")
    if settings.public_scheme == "https":
        if cert_path is None or key_path is None:
            raise ValueError("HTTPS requires a TLS certificate and private key")
        _validate_tls_files(cert_path, key_path)


def _replace_env(content: str, updates: dict[str, str]) -> str:
    lines = content.splitlines(keepends=True)
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        match = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", line)
        if match and match.group(1) in updates:
            key = match.group(1)
            output.append(f"{key}={_dotenv(updates[key])}\n")
            seen.add(key)
        else:
            output.append(line)
    for key, value in updates.items():
        if key not in seen:
            output.append(f"{key}={_dotenv(value)}\n")
    return "".join(output)


def _atomic_write(path: Path, content: str) -> None:
    temporary = path.with_name(f".{path.name}.network.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        shutil.copystat(path, temporary, follow_symlinks=False)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_copy(source: Path, target: Path, mode: int) -> None:
    temporary = target.with_name(f".{target.name}.network.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, temporary)
        os.chmod(temporary, mode)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _backup(path: Path) -> Path:
    backup = path.with_name(path.name + ".bak")
    if path.is_file():
        shutil.copy2(path, backup)
    else:
        backup.unlink(missing_ok=True)
    return backup


def _snapshot(path: Path) -> tuple[bytes, int] | None:
    if not path.is_file():
        return None
    return path.read_bytes(), path.stat().st_mode & 0o777


def _restore_snapshot(path: Path, snapshot: tuple[bytes, int] | None) -> None:
    if snapshot is None:
        path.unlink(missing_ok=True)
        return
    content, mode = snapshot
    temporary = path.with_name(f".{path.name}.network.rollback.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(content)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _certificate_targets(deployment: DeploymentContext, server_dir: Path) -> tuple[Path, Path]:
    values = read_deployment_env(deployment.env_file)
    raw_root = values.get("CERTS_DIR")
    if not raw_root:
        raise ValueError("CERTS_DIR is missing from the deployment environment")
    root = Path(raw_root)
    if not root.is_absolute():
        root = (server_dir / root).resolve()
    return root / "tls.crt", root / "tls.key"


def _replace_tls_material(
    deployment: DeploymentContext,
    server_dir: Path,
    certificate: Path | None,
    private_key: Path | None,
) -> tuple[Path, Path] | None:
    if certificate is None and private_key is None:
        return None
    if certificate is None or private_key is None:
        raise ValueError("both TLS certificate and private key are required")
    certificate, private_key = _validate_tls_files(certificate, private_key)
    target_certificate, target_private_key = _certificate_targets(deployment, server_dir)
    _backup(target_certificate)
    _backup(target_private_key)
    if certificate.resolve() != target_certificate.resolve():
        _atomic_copy(certificate, target_certificate, 0o644)
    if private_key.resolve() != target_private_key.resolve():
        _atomic_copy(private_key, target_private_key, 0o600)
    return target_certificate, target_private_key


def _validate_compose_configuration(server_dir: Path, env_file: Path) -> None:
    result = ComposeAdapter(server_dir, env_file).run("config", capture=True)
    if result.returncode:
        detail = (result.stderr or result.stdout or "docker compose config failed").strip()
        raise RuntimeError(detail)


def save_network_settings(settings: NetworkSettings, env_file: Path, config_file: Path, server_dir: Path, *, cert_path: Path | None = None, key_path: Path | None = None) -> None:
    validate_network_settings(settings, cert_path=cert_path, key_path=key_path)
    env_content = env_file.read_text(encoding="utf-8")
    rtsp_public_host = settings.rtsp_bind_address
    if rtsp_public_host in {"0.0.0.0", "::"}:
        rtsp_public_host = urlsplit(settings.public_base_url).hostname or "127.0.0.1"
    updates = {
        "PUBLIC_SCHEME": settings.public_scheme,
        "PUBLIC_BIND_ADDRESS": settings.public_bind_address,
        "PUBLIC_BASE_URL": settings.public_base_url,
        "PUBLIC_HTTP_PORT": str(settings.public_http_port),
        "PUBLIC_HTTPS_PORT": str(settings.public_https_port),
        "RTSP_BIND_ADDRESS": settings.rtsp_bind_address,
        "RTSP_PORT": str(settings.rtsp_port),
        "RTSP_PUBLIC_HOST": rtsp_public_host,
        "RTSP_PUBLIC_PORT": str(settings.rtsp_port),
        "ALLOW_INSECURE_HTTP": str(settings.allow_insecure_http).lower(),
        "COOKIE_SECURE": str(settings.cookie_secure).lower(),
        "NGINX_CONFIG_FILE": str(server_dir / "services" / "nginx" / ("nginx.http.conf" if settings.public_scheme == "http" else "nginx.conf")),
    }
    config = yaml.safe_load(config_file.read_text(encoding="utf-8"))
    if not isinstance(config, dict) or not isinstance(config.get("server"), dict):
        raise ValueError("config.yaml does not contain a server section")
    updated_config = dict(config)
    updated_server = dict(config["server"])
    updated_server.update({
        "public_http_port": settings.public_http_port,
        "public_https_port": settings.public_https_port,
        "rtsp_bind_address": settings.rtsp_bind_address,
        "rtsp_port": settings.rtsp_port,
    })
    updated_config["server"] = updated_server
    config_content = yaml.safe_dump(updated_config, allow_unicode=True, sort_keys=False)
    for path in (env_file, config_file):
        _backup(path)
    _atomic_write(env_file, _replace_env(env_content, updates))
    _atomic_write(config_file, config_content)


def apply_network_settings(
    settings: NetworkSettings,
    deployment: DeploymentContext,
    server_dir: Path,
    progress: Callable[[str], None],
    *,
    cert_path: Path | None = None,
    key_path: Path | None = None,
) -> str:
    """Persist network settings and recreate services through the operations layer."""

    from .services import run_service_action

    tracked_files = [deployment.env_file, deployment.config_file]
    if cert_path is not None or key_path is not None:
        tracked_files.extend(_certificate_targets(deployment, server_dir))
    snapshot = {path: _snapshot(path) for path in tracked_files}
    services_may_have_changed = False
    try:
        save_network_settings(
            settings,
            deployment.env_file,
            deployment.config_file,
            server_dir,
            cert_path=cert_path,
            key_path=key_path,
        )
        _replace_tls_material(deployment, server_dir, cert_path, key_path)
        progress("변경한 Compose 구성을 검증하고 있습니다…")
        _validate_compose_configuration(server_dir, deployment.env_file)
        services_may_have_changed = True
        return run_service_action(server_dir, deployment, "start", progress)
    except Exception as exc:
        for path, previous in snapshot.items():
            _restore_snapshot(path, previous)
        try:
            if services_may_have_changed:
                progress("이전 설정을 복원하고 서버를 다시 적용하고 있습니다…")
                run_service_action(server_dir, deployment, "start", progress)
        except Exception as rollback_exc:
            raise RuntimeError(
                f"network update failed: {exc}; rollback could not be applied: {rollback_exc}"
            ) from exc
        raise
