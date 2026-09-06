"""Serialize camera registration, admission and control without an unbounded registry."""

import asyncio
import hashlib


class CameraLifecycleLocks:
    def __init__(self) -> None:
        # MediaMTX can supply attacker-selected camera names before authentication.
        # Hash collisions add safe serialization while keeping memory bounded.
        self._locks = tuple(asyncio.Lock() for _ in range(64))

    def __call__(self, camera_id: str) -> asyncio.Lock:
        digest = hashlib.sha256(camera_id.encode("utf-8")).digest()
        return self._locks[int.from_bytes(digest[:8], "big") % len(self._locks)]
