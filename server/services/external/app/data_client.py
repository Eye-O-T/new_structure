from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx


class DataServiceError(Exception):
    status_code = 502

    def __init__(
        self,
        message: str = "data service request failed",
        *,
        code: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code


class DataServiceUnavailable(DataServiceError):
    status_code = 503


class DataNotFound(DataServiceError):
    status_code = 404


class DataForbidden(DataServiceError):
    status_code = 403


class DataConflict(DataServiceError):
    status_code = 409


class DataClient:
    def __init__(
        self,
        *,
        base_url: str,
        health_url: str,
        internal_token: str,
        timeout_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.health_url = health_url
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers={"X-Internal-Token": internal_token},
            timeout=httpx.Timeout(timeout_seconds),
            transport=transport,
            # Internal service traffic must not be diverted through host proxy
            # settings. This also keeps the shared internal token on the
            # private Docker network selected by DATA_BASE_URL.
            trust_env=False,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        not_found_ok: bool = False,
    ) -> Any:
        clean_params = None
        if params is not None:
            clean_params = {
                key: value for key, value in params.items() if value is not None
            }

        try:
            response = await self._client.request(
                method,
                path.lstrip("/") if not path.startswith("http") else path,
                params=clean_params,
                json=json,
            )
        except httpx.RequestError as exc:
            raise DataServiceUnavailable("data service unavailable") from exc

        if response.status_code == 404 and not_found_ok:
            return None
        if response.status_code == 404:
            raise DataNotFound("resource not found")
        if response.status_code in {401, 403}:
            raise DataForbidden("data service denied the request")
        if response.status_code == 409:
            code = None
            message = "resource conflict"
            try:
                error = response.json().get("error", {})
                candidate_code = error.get("code")
                candidate_message = error.get("message")
                if candidate_code in {
                    "CAMERA_HAS_HISTORY",
                    "CAMERA_LIMIT_REACHED",
                }:
                    code = str(candidate_code)
                    if isinstance(candidate_message, str) and candidate_message:
                        message = candidate_message
            except (AttributeError, ValueError):
                pass
            raise DataConflict(message, code=code)
        if response.status_code >= 500:
            raise DataServiceUnavailable("data service unavailable")
        if response.status_code >= 400:
            raise DataServiceError("data service rejected the request")
        if response.status_code == 204 or not response.content:
            return None

        try:
            return response.json()
        except ValueError as exc:
            raise DataServiceError("data service returned an invalid response") from exc

    async def health(self) -> Any:
        return await self._request("GET", self.health_url)

    async def get_user_by_username(self, username: str) -> dict[str, Any]:
        return await self._request(
            "GET", f"users/by-username/{quote(username, safe='')}"
        )

    async def list_users(self, *, limit: int, offset: int) -> Any:
        return await self._request(
            "GET", "users", params={"limit": limit, "offset": offset}
        )

    async def create_user(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "users", json=payload)

    async def get_user(self, user_id: str) -> dict[str, Any]:
        return await self._request("GET", f"users/{quote(str(user_id), safe='')}")

    async def update_user(
        self, user_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._request(
            "PATCH",
            f"users/{quote(str(user_id), safe='')}",
            json=payload,
        )

    async def get_camera_permissions(self, user_id: str) -> Any:
        return await self._request(
            "GET",
            f"users/{quote(str(user_id), safe='')}/camera-permissions",
        )

    async def set_camera_permissions(self, user_id: str, camera_ids: list[str]) -> Any:
        return await self._request(
            "PUT",
            f"users/{quote(str(user_id), safe='')}/camera-permissions",
            json={"camera_ids": camera_ids},
        )

    async def create_refresh_token(self, payload: dict[str, Any]) -> Any:
        return await self._request("POST", "tokens/refresh", json=payload)

    async def rotate_refresh_token(self, old_jti: str, payload: dict[str, Any]) -> Any:
        rotation_payload = dict(payload)
        rotation_payload["rotated_from_jti"] = old_jti
        return await self._request("POST", "tokens/refresh", json=rotation_payload)

    async def get_refresh_token(self, jti: str) -> dict[str, Any]:
        return await self._request("GET", f"tokens/refresh/{quote(jti, safe='')}")

    async def revoke_refresh_token(self, jti: str) -> None:
        await self._request("DELETE", f"tokens/refresh/{quote(jti, safe='')}")

    async def is_access_token_revoked(self, jti: str) -> bool:
        result = await self._request(
            "GET",
            f"tokens/revoked/{quote(jti, safe='')}",
            not_found_ok=True,
        )
        if result is None:
            return False
        if isinstance(result, dict) and "revoked" in result:
            return bool(result["revoked"])
        return True

    async def revoke_access_token(self, jti: str, payload: dict[str, Any]) -> None:
        await self._request(
            "PUT",
            f"tokens/revoked/{quote(jti, safe='')}",
            json=payload,
        )

    async def list_cameras(self, *, user_id: str, limit: int, offset: int) -> Any:
        return await self._request(
            "GET",
            "cameras",
            params={"user_id": user_id, "limit": limit, "offset": offset},
        )

    async def create_camera(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "cameras", json=payload)

    async def get_camera(self, camera_id: str, *, user_id: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"cameras/{quote(camera_id, safe='')}",
            params={"user_id": user_id},
        )

    async def update_camera(
        self, camera_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._request(
            "PATCH",
            f"cameras/{quote(camera_id, safe='')}",
            json=payload,
        )

    async def delete_camera(self, camera_id: str) -> None:
        await self._request("DELETE", f"cameras/{quote(camera_id, safe='')}")

    async def get_camera_deletion_status(self, camera_id: str) -> dict[str, Any]:
        return await self._request(
            "GET", f"cameras/{quote(camera_id, safe='')}/deletion-status"
        )

    async def put_camera_publish_credential(
        self, camera_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._request(
            "PUT",
            f"cameras/{quote(camera_id, safe='')}/publish-credential",
            json=payload,
        )

    async def get_camera_publish_credential(
        self, camera_id: str
    ) -> dict[str, Any] | None:
        return await self._request(
            "GET",
            f"cameras/{quote(camera_id, safe='')}/publish-credential",
            not_found_ok=True,
        )

    async def list_camera_control_targets(self) -> Any:
        return await self._request("GET", "camera-control-targets")

    async def get_camera_control_target(self, camera_id: str) -> dict[str, Any]:
        return await self._request(
            "GET", f"cameras/{quote(camera_id, safe='')}/control-target"
        )

    async def get_camera_video_profile(self, camera_id: str) -> dict[str, Any]:
        return await self._request(
            "GET", f"cameras/{quote(camera_id, safe='')}/video-profile"
        )

    async def update_camera_video_profile(
        self, camera_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._request(
            "PATCH",
            f"cameras/{quote(camera_id, safe='')}/video-profile",
            json=payload,
        )

    async def get_camera_runtime_status(self, camera_id: str) -> dict[str, Any]:
        return await self._request(
            "GET", f"cameras/{quote(camera_id, safe='')}/runtime-status"
        )

    async def put_camera_runtime_status(
        self, camera_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        return await self._request(
            "PUT",
            f"cameras/{quote(camera_id, safe='')}/runtime-status",
            json=payload,
        )

    async def create_event(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("POST", "events", json=payload)

    async def list_recovery_jobs(
        self, *, camera_id: str | None, limit: int, offset: int
    ) -> Any:
        return await self._request(
            "GET",
            "recovery-jobs",
            params={"camera_id": camera_id, "limit": limit, "offset": offset},
        )

    async def list_recordings(self, **params: Any) -> Any:
        return await self._request(
            "GET",
            "recording-segments/search",
            params={
                "camera_id": params.get("camera_id"),
                "from": params.get("start"),
                "to": params.get("end"),
                "limit": params.get("limit"),
                "offset": params.get("offset"),
            },
        )

    async def get_recording(self, segment_id: str, *, user_id: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"recording-segments/{quote(segment_id, safe='')}",
            params={"user_id": user_id},
        )

    async def open_recording_content(
        self,
        segment_id: str,
        *,
        range_header: str | None = None,
        if_range_header: str | None = None,
    ) -> httpx.Response:
        headers = {
            name: value
            for name, value in {
                "Range": range_header,
                "If-Range": if_range_header,
            }.items()
            if value
        }
        request = self._client.build_request(
            "GET",
            f"recording-segments/{quote(segment_id, safe='')}/content",
            headers=headers or None,
        )
        try:
            response = await self._client.send(request, stream=True)
        except httpx.RequestError as exc:
            raise DataServiceUnavailable("data service unavailable") from exc
        if response.status_code in {200, 206, 416}:
            return response
        await response.aread()
        await response.aclose()
        if response.status_code == 404:
            raise DataNotFound("recording content not found")
        if response.status_code in {401, 403}:
            raise DataForbidden("data service denied the request")
        if response.status_code >= 500:
            raise DataServiceUnavailable("data service unavailable")
        raise DataServiceError("data service rejected the request")

    async def list_events(self, **params: Any) -> Any:
        return await self._request(
            "GET",
            "events",
            params={
                "camera_id": params.get("camera_id"),
                "event_type": params.get("event_type"),
                "from": params.get("start"),
                "to": params.get("end"),
                "limit": params.get("limit"),
                "offset": params.get("offset"),
            },
        )

    async def get_event(self, event_id: str, *, user_id: str) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"events/{quote(event_id, safe='')}",
            params={"user_id": user_id},
        )
