"""Fail-closed, Edge-owned nftables policy management."""
from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import EdgeConfig
from .network import selected_interface_ipv4

try:
    import fcntl
    import pwd
except ImportError:  # pragma: no cover - Windows development environment
    fcntl = None  # type: ignore[assignment]
    pwd = None  # type: ignore[assignment]

TABLE = "ai_cctv_edge"
POLICY_DIR = Path("/run/ai-cctv-edge-network")
LOCK_PATH = POLICY_DIR / "policy.lock"
STATE_PATH = POLICY_DIR / "applied.json"
INTERFACE_NAME = re.compile(r"^[A-Za-z0-9_.:-]{1,15}$")


@contextmanager
def policy_lock() -> Iterator[None]:
    if os.name != "posix" or fcntl is None or os.geteuid() != 0:
        raise PermissionError("Edge network policy requires root")
    POLICY_DIR.mkdir(mode=0o750, parents=True, exist_ok=True)
    os.chown(POLICY_DIR, 0, _service_gid())
    os.chmod(POLICY_DIR, 0o750)
    descriptor = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        with os.fdopen(descriptor, "r+") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            yield
    finally:
        try:
            os.close(descriptor)
        except OSError:
            pass


def _service_uid() -> int:
    if pwd is None:
        raise RuntimeError("Edge network policy is only supported on Linux")
    try:
        return pwd.getpwnam("ai-cctv-edge").pw_uid
    except KeyError as exc:
        raise RuntimeError("ai-cctv-edge service account is missing") from exc


def _service_gid() -> int:
    if pwd is None:
        raise RuntimeError("Edge network policy is only supported on Linux")
    try:
        return pwd.getpwnam("ai-cctv-edge").pw_gid
    except KeyError as exc:
        raise RuntimeError("ai-cctv-edge service account is missing") from exc


def _interface(interface: str) -> str:
    if not INTERFACE_NAME.fullmatch(interface) or interface == "lo":
        raise ValueError("invalid selected network interface")
    return interface


def render_ruleset(
    *,
    interface: str,
    ports: tuple[int, ...],
    service_uid: int,
    pairing: bool = False,
) -> str:
    name = _interface(interface)
    if not ports or any(isinstance(port, bool) or not 1 <= port <= 65535 for port in ports):
        raise ValueError("invalid Edge service ports")
    if not 0 <= service_uid <= 2**32 - 1:
        raise ValueError("invalid Edge service UID")
    unique_ports = tuple(dict.fromkeys(ports))
    port_set = ", ".join(str(port) for port in unique_ports)
    if pairing:
        address = selected_interface_ipv4(name)
        return "\n".join([
            f"table inet {TABLE} {{",
            "  chain input {",
            "    type filter hook input priority -20; policy accept;",
            f"    iifname \"{name}\" tcp dport {unique_ports[0]} accept",
            f"    tcp dport {unique_ports[0]} drop",
            "  }",
            "  chain output {",
            "    type filter hook output priority -20; policy accept;",
            f"    meta skuid 0 tcp sport {unique_ports[0]} oifname \"{name}\" accept",
            f"    meta skuid 0 tcp sport {unique_ports[0]} drop",
            f"    meta skuid 0 udp dport 37020 oifname \"{name}\" ip daddr {address.broadcast} accept",
            "    meta skuid 0 udp dport 37020 drop",
            "  }",
            "}",
            "",
        ])
    # Preserve unrelated conntrack marks and constrain both request and response paths.
    lines = [
        f"table inet {TABLE} {{",
        "  chain input {",
        "    type filter hook input priority -20; policy accept;",
        f"    iifname \"{name}\" tcp dport {{ {port_set} }} accept",
        f"    tcp dport {{ {port_set} }} drop",
        f"    ct mark & 0x40000000 != 0 iifname != \"{name}\" drop",
        "  }",
        "  chain output {",
        "    type route hook output priority -20; policy accept;",
        f"    meta skuid {service_uid} ip daddr 127.0.0.0/8 accept",
        f"    meta skuid {service_uid} ip daddr 0.0.0.0/32 accept",
        f"    meta skuid {service_uid} meta nfproto ipv4 ct mark set ct mark | 0x40000000",
        f"    meta skuid {service_uid} meta nfproto ipv4 oifname \"{name}\" accept",
        f"    meta skuid {service_uid} meta nfproto ipv6 drop",
        f"    meta skuid {service_uid} meta nfproto ipv4 oifname != \"{name}\" drop",
        "  }",
        "}",
    ]
    return "\n".join(lines) + "\n"


def _apply(script: str) -> None:
    check = subprocess.run(
        ["nft", "--check", "-f", "-"], input=script, text=True,
        capture_output=True, timeout=10, check=False,
    )
    if check.returncode:
        raise RuntimeError(f"nft ruleset validation failed: {check.stderr.strip()}")
    applied = subprocess.run(
        ["nft", "-f", "-"], input=script, text=True,
        capture_output=True, timeout=10, check=False,
    )
    if applied.returncode:
        raise RuntimeError(f"nft ruleset application failed: {applied.stderr.strip()}")


def apply_persistent(config: EdgeConfig) -> None:
    if not config.network.interface:
        _apply_local_only(config)
        return
    interface = _interface(config.network.interface)
    ports = [config.control.port, config.recovery.port]
    if config.rtsp.mode == "central_pull":
        ports.append(config.rtsp.edge_port)
    with policy_lock():
        script = f"destroy table inet {TABLE}\n" + render_ruleset(
            interface=interface,
            ports=tuple(ports),
            service_uid=_service_uid(),
        )
        _apply(script)
        show = subprocess.run(
            ["nft", "list", "table", "inet", TABLE],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if show.returncode or f'meta skuid {_service_uid()}' not in show.stdout:
            raise RuntimeError("NETWORK_POLICY_UNAVAILABLE: installed policy did not verify")
        if f"ct mark set ct mark | 0x40000000" not in show.stdout:
            raise RuntimeError("NETWORK_POLICY_UNAVAILABLE: response path rule is missing")
        state = {
            "interface": interface,
            "ports": ports,
            "uid": _service_uid(),
            "mode": config.rtsp.mode,
        }
        temporary = STATE_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
        os.chown(temporary, 0, _service_gid())
        os.chmod(temporary, 0o440)
        os.replace(temporary, STATE_PATH)


def _apply_local_only(config: EdgeConfig) -> None:
    uid = _service_uid()
    ports = [config.control.port, config.recovery.port]
    if config.rtsp.mode == "central_pull":
        ports.append(config.rtsp.edge_port)
    ports = list(dict.fromkeys(ports))
    with policy_lock():
        script = f"""destroy table inet {TABLE}
table inet {TABLE} {{
  chain input {{
    type filter hook input priority -20; policy accept;
  }}
  chain output {{
    type filter hook output priority -20; policy accept;
    meta skuid {uid} ip daddr 127.0.0.0/8 accept
    meta skuid {uid} ip daddr 0.0.0.0/32 accept
    meta skuid {uid} drop
  }}
}}
"""
        _apply(script)
        shown = subprocess.run(
            ["nft", "list", "table", "inet", TABLE],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if shown.returncode or f"meta skuid {uid} drop" not in shown.stdout:
            raise RuntimeError("NETWORK_POLICY_UNAVAILABLE: local-only policy did not verify")
        state = {"interface": None, "ports": ports, "uid": uid, "mode": "local-only"}
        temporary = STATE_PATH.with_suffix(".tmp")
        temporary.write_text(json.dumps(state, sort_keys=True) + "\n", encoding="utf-8")
        os.chown(temporary, 0, _service_gid())
        os.chmod(temporary, 0o440)
        os.replace(temporary, STATE_PATH)


def apply_pairing(interface: str, address: str, port: int) -> None:
    ipaddress.IPv4Address(address)
    with policy_lock():
        script = f"destroy table inet {TABLE}\n" + render_ruleset(
            interface=interface, ports=(port,), service_uid=0, pairing=True
        )
        _apply(script)


def verify_persistent(config: EdgeConfig) -> None:
    interface = _interface(config.network.interface) if config.network.interface else None
    if os.name == "posix" and os.geteuid() == 0:
        with policy_lock():
            result = subprocess.run(
                ["nft", "list", "table", "inet", TABLE],
                capture_output=True, text=True, timeout=5, check=False,
            )
        if result.returncode:
            raise RuntimeError("NETWORK_POLICY_UNAVAILABLE: nftables table is missing")
        if interface and (
            f'iifname "{interface}"' not in result.stdout
            or f"meta skuid {_service_uid()}" not in result.stdout
        ):
            raise RuntimeError("NETWORK_POLICY_UNAVAILABLE: installed policy does not match configuration")
        if not interface and f"meta skuid {_service_uid()} drop" not in result.stdout:
            raise RuntimeError("NETWORK_POLICY_UNAVAILABLE: local-only policy is missing")
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("NETWORK_POLICY_UNAVAILABLE: root policy state is missing") from exc
    else:
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("NETWORK_POLICY_UNAVAILABLE: root policy state is missing") from exc
    ports = [config.control.port, config.recovery.port]
    if config.rtsp.mode == "central_pull":
        ports.append(config.rtsp.edge_port)
    expected_mode = config.rtsp.mode if interface else "local-only"
    if state != {
        "interface": interface,
        "ports": ports,
        "uid": _service_uid(),
        "mode": expected_mode,
    }:
        raise RuntimeError("NETWORK_POLICY_UNAVAILABLE: installed policy does not match configuration")


def remove_policy() -> None:
    with policy_lock():
        result = subprocess.run(
            ["nft", "destroy", "table", "inet", TABLE],
            capture_output=True, text=True, timeout=5, check=False,
        )
        if result.returncode and "No such file" not in result.stderr:
            raise RuntimeError(f"could not remove Edge network policy: {result.stderr.strip()}")
        STATE_PATH.unlink(missing_ok=True)
