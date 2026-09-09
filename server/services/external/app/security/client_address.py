"""Resolve only explicitly trusted proxy peers before using their client IP header."""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time

from fastapi import Request


class TrustedProxyAddresses:
    def __init__(self, hosts: tuple[str, ...], *, cache_seconds: float = 30) -> None:
        self.hosts = hosts
        self.cache_seconds = cache_seconds
        self._addresses: set[str] = set()
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def _resolve(self) -> set[str]:
        if time.monotonic() < self._expires_at:
            return self._addresses
        async with self._lock:
            if time.monotonic() < self._expires_at:
                return self._addresses
            addresses: set[str] = set()
            for host in self.hosts:
                try:
                    addresses.add(str(ipaddress.ip_address(host)))
                    continue
                except ValueError:
                    pass
                try:
                    results = await asyncio.wait_for(
                        asyncio.get_running_loop().getaddrinfo(
                            host,
                            None,
                            type=socket.SOCK_STREAM,
                        ),
                        timeout=1,
                    )
                except (OSError, TimeoutError):
                    # DNS 실패 때 이전 컨테이너 주소에 대한 신뢰를 연장하지 않는다.
                    continue
                for _family, _kind, _protocol, _name, address in results:
                    addresses.add(str(ipaddress.ip_address(address[0])))
            self._addresses = addresses
            self._expires_at = time.monotonic() + self.cache_seconds
            return addresses

    async def client_address(self, request: Request) -> str:
        peer = request.client.host if request.client else "unknown"
        forwarded = request.headers.getlist("x-real-ip")
        if len(forwarded) != 1:
            return peer
        try:
            original = str(ipaddress.ip_address(forwarded[0]))
            normalized_peer = str(ipaddress.ip_address(peer))
        except ValueError:
            return peer
        if normalized_peer in await self._resolve():
            return original
        return peer
