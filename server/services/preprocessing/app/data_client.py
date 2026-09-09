# 탐지 서비스가 Data에 카메라 상태·좌표·이벤트를 전달하는 HTTP 통신 창구다.
# 데이터베이스 파일을 직접 열지 않고 Data API의 검증·저장 규칙을 따른다.
from __future__ import annotations

from typing import Any

import httpx


# 탐지 전용 내부 토큰을 재사용하는 동기 HTTP 클라이언트다.
class DataClient:
    def __init__(self, base_url: str, token: str, timeout: float = 10.0):
        self._client = httpx.Client(
            base_url=base_url,
            headers={"X-Internal-Token": token},
            timeout=timeout,
            # 호스트의 프록시 환경변수를 따르지 않아 내부 인증 토큰이 외부 프록시로 새지 않게 한다.
            trust_env=False,
        )

    def close(self) -> None:
        self._client.close()

    # 카메라 목록 조회의 성공 여부로 인증을 포함한 Data 접근 가능성을 확인한다.
    def ready(self) -> bool:
        try:
            response = self._client.get("/cameras/enabled", timeout=3.0)
            return response.is_success
        except httpx.HTTPError:
            return False

    # 리스트 본문과 items/cameras 래퍼 형식을 모두 활성 카메라 목록으로 정규화한다.
    def enabled_cameras(self) -> list[dict[str, Any]]:
        response = self._client.get("/cameras/enabled", timeout=5.0)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            payload = payload.get("items", payload.get("cameras"))
        if not isinstance(payload, list) or any(
            not isinstance(row, dict) for row in payload
        ):
            raise ValueError("Data returned an invalid enabled camera list")
        return payload

    def set_camera_status(self, camera_id: str, status: str) -> None:
        response = self._client.patch(
            f"/cameras/{camera_id}/status", json={"status": status}, timeout=2.0
        )
        response.raise_for_status()

    def put_live_objects(self, camera_id: str, payload: dict) -> None:
        # 실시간 좌표는 오래 기다리면 가치가 줄어 일반 요청보다 짧은 시간제한을 적용한다.
        response = self._client.put(
            f"/cameras/{camera_id}/objects", json=payload, timeout=2.0
        )
        response.raise_for_status()

    # Data가 이벤트를 수락하지 않으면 호출자가 전송 실패를 기록할 수 있도록 예외를 전달한다.
    def create_event(self, event: dict[str, Any]) -> None:
        response = self._client.post("/events", json=event, timeout=5.0)
        response.raise_for_status()
