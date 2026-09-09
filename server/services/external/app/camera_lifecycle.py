# 같은 카메라의 등록·인증·제어가 겹쳐 상태가 엇갈리지 않도록 잠금을 제공한다.

import asyncio
import hashlib


class CameraLifecycleLocks:
    def __init__(self) -> None:
        # 인증 전 임의의 카메라 이름이 들어와도 고정 개수의 잠금만 쓴다. 해시 충돌은 추가 대기만 만든다.
        self._locks = tuple(asyncio.Lock() for _ in range(64))

    # 같은 카메라 ID를 항상 같은 잠금으로 보내 등록·제어·인증의 순서를 공유한다.
    def __call__(self, camera_id: str) -> asyncio.Lock:
        digest = hashlib.sha256(camera_id.encode("utf-8")).digest()
        return self._locks[int.from_bytes(digest[:8], "big") % len(self._locks)]
