from __future__ import annotations

from typing import Any

import httpx


class DataClient:
    def __init__(self, base_url: str, token: str, timeout: float = 10.0):
        self._client = httpx.Client(
            base_url=base_url,
            headers={"X-Internal-Token": token},
            timeout=timeout,
            # Never forward the scoped Data token to a host/container proxy.
            trust_env=False,
        )

    def close(self) -> None:
        self._client.close()

    def ready(self) -> bool:
        try:
            response = self._client.get("/cameras/enabled", timeout=3.0)
            return response.is_success
        except httpx.HTTPError:
            return False

    def enabled_cameras(self) -> list[dict[str, Any]]:
        response = self._client.get("/cameras/enabled")
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, list):
            return payload
        return payload.get("items", payload.get("cameras", []))

    def set_camera_status(self, camera_id: str, status: str) -> None:
        response = self._client.patch(
            f"/cameras/{camera_id}/status", json={"status": status}
        )
        response.raise_for_status()

    def create_event(self, event: dict[str, Any]) -> None:
        response = self._client.post("/events", json=event)
        response.raise_for_status()
