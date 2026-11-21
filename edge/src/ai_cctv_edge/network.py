"""Small Linux network helpers used by discovery and the Edge doctor."""
from __future__ import annotations

import ipaddress
import json
import os
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


def interface_ipv4_addresses(interface: str) -> tuple[InterfaceIPv4, ...]:
    """Return every usable IPv4 address reported by Linux for an interface."""
    try:
        result = subprocess.run(
            ["ip", "-j", "-4", "addr", "show", "dev", interface],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise ValueError("the ip command could not inspect the interface") from exc
    if result.returncode != 0:
        raise ValueError(f"network interface does not exist: {interface}")
    try:
        rows = json.loads(result.stdout)
        addresses = [
            network_details(interface, item["local"], item["prefixlen"])
            for row in rows
            for item in row.get("addr_info", [])
            if item.get("family") == "inet"
            and item.get("scope") == "global"
            and not item.get("local", "").startswith("169.254.")
        ]
    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise ValueError("could not parse interface IPv4 addresses") from exc
    if not addresses:
        raise ValueError(f"network interface has no usable IPv4 address: {interface}")
    if len(addresses) != 1:
        raise ValueError(f"network interface has multiple usable IPv4 addresses: {interface}")
    return tuple(addresses)


def selected_interface_ipv4(interface: str) -> InterfaceIPv4:
    """Validate and return the single global IPv4 selected for Edge services."""
    if interface == "lo" or not interface or len(interface) >= 16 or any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.:-"
        for character in interface
    ):
        raise ValueError(f"invalid network interface: {interface!r}")
    addresses = interface_ipv4_addresses(interface)
    return addresses[0]


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


def route_details(destination: str, *, interface: str, uid: int | None = None) -> dict[str, str]:
    """Return a route only when Linux selects the configured interface."""
    command = ["ip", "-j", "-4", "route", "get", destination]
    if uid is not None:
        command.extend(["uid", str(uid)])
    try:
        result = subprocess.run(
            command, capture_output=True, text=True, timeout=5, check=False
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise ValueError("the ip command could not inspect the route") from exc
    if result.returncode != 0:
        raise ValueError(f"no route to {destination}")
    try:
        rows = json.loads(result.stdout)
        route = rows[0]
        selected = route["dev"]
        source = route.get("prefsrc", route.get("src"))
        if not source:
            raise KeyError("prefsrc")
    except (json.JSONDecodeError, KeyError, IndexError, TypeError) as exc:
        raise ValueError(f"route to {destination} has incomplete details") from exc
    if selected != interface:
        raise ValueError(f"route to {destination} uses {selected}, expected {interface}")
    return {"interface": selected, "source": source}
