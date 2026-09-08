# 같은 카메라의 등록·인증·제어가 겹쳐 상태가 엇갈리지 않도록 잠금을 제공한다.
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
