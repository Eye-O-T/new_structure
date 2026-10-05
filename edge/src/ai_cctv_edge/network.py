"""Small Linux network helpers used by discovery and the Edge doctor."""
from __future__ import annotations

import ipaddress
import socket
import struct
import subprocess
from dataclasses import dataclass


@dataclass(frozen=True)
class InterfaceIPv4:
    name: str
    address: str
    prefix: int
    broadcast: str

def interface_mac(interface: str) -> str:
    if not interface or len(interface) >= 16 or any(c in interface for c in "\x00\r\n"):
        raise ValueError(f"invalid network interface: {interface!r}")
    try:
        raw = open(f"/sys/class/net/{interface}/address", encoding="ascii").read().strip()
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"network interface has no MAC address: {interface}") from exc
    parts = raw.split(":")
    if len(parts) != 6:
        raise ValueError("invalid interface MAC address")
    try:
        octets = [int(part, 16) for part in parts]
    except ValueError as exc:
        raise ValueError("invalid interface MAC address") from exc
    if any(not 0 <= value <= 255 for value in octets) or all(value == 0 for value in octets):
        raise ValueError("invalid interface MAC address")
    return ":".join(f"{value:02x}" for value in octets)


def network_details(name: str, address: str, netmask: str) -> InterfaceIPv4:
    network = ipaddress.IPv4Network(f"{address}/{netmask}", strict=False)
    return InterfaceIPv4(name, address, network.prefixlen, str(network.broadcast_address))


def interface_ipv4(interface: str) -> InterfaceIPv4:
    """Return the first IPv4 address and directed broadcast for *interface*."""
    if not interface or len(interface) >= 16 or any(
        character in interface for character in "\x00\r\n"
    ):
        raise ValueError(f"invalid network interface: {interface!r}")
    try:
        socket.if_nametoindex(interface)
    except OSError as exc:
        raise ValueError(f"network interface does not exist: {interface}") from exc
    try:
        import fcntl
    except ImportError as exc:  # pragma: no cover - Edge runs on Linux
        raise ValueError("interface IPv4 lookup requires Linux") from exc

    request = struct.pack("256s", interface.encode("ascii"))
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            address = socket.inet_ntoa(
                fcntl.ioctl(sock.fileno(), 0x8915, request)[20:24]
            )
            netmask = socket.inet_ntoa(
                fcntl.ioctl(sock.fileno(), 0x891b, request)[20:24]
            )
    except (UnicodeEncodeError, OSError) as exc:
        raise ValueError(f"network interface has no IPv4 address: {interface}") from exc

    return network_details(interface, address, netmask)


def route_interface(destination: str) -> str:
    """Return the Linux interface selected for an IPv4 destination."""
    try:
        result = subprocess.run(
            ["ip", "-4", "route", "get", destination],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise ValueError("the ip command could not inspect the route") from exc
    if result.returncode != 0:
        raise ValueError(f"no route to {destination}")
    fields = result.stdout.split()
    try:
        return fields[fields.index("dev") + 1]
    except (ValueError, IndexError) as exc:
        raise ValueError(f"route to {destination} has no interface") from exc
