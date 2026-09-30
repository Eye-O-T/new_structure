"""Read, validate, and safely update network settings of an existing install."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from server.setup.config_core import _dotenv, _validate_public_base_url, _validate_tls_files
from server.setup.validation import read_deployment_env


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


def save_network_settings(settings: NetworkSettings, env_file: Path, config_file: Path, server_dir: Path, *, cert_path: Path | None = None, key_path: Path | None = None) -> None:
    validate_network_settings(settings, cert_path=cert_path, key_path=key_path)
    env_content = env_file.read_text(encoding="utf-8")
    updates = {
        "PUBLIC_SCHEME": settings.public_scheme,
        "PUBLIC_BIND_ADDRESS": settings.public_bind_address,
        "PUBLIC_BASE_URL": settings.public_base_url,
        "PUBLIC_HTTP_PORT": str(settings.public_http_port),
        "PUBLIC_HTTPS_PORT": str(settings.public_https_port),
        "RTSP_BIND_ADDRESS": settings.rtsp_bind_address,
        "RTSP_PORT": str(settings.rtsp_port),
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
        backup = path.with_name(path.name + ".bak")
        shutil.copy2(path, backup)
    _atomic_write(env_file, _replace_env(env_content, updates))
    _atomic_write(config_file, config_content)
