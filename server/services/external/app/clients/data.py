# External이 SQLite에 직접 접근하지 않고 인증된 HTTP 요청으로 Data 기능을 이용하게 한다.
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


# 인증 헤더·연결 풀·오류 변환을 공유하는 Data 내부 API 접근 경계이다.
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
            # 내부 토큰이 호스트 프록시로 새지 않도록 DATA_BASE_URL에 직접 접속한다.
            trust_env=False,
        )

    async def close(self) -> None:
        await self._client.aclose()

    # 생략할 질의값을 제거하고 내부 HTTP 오류를 제한된 공개 예외로 바꾸며 빈 응답도 처리한다.
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

    async def put_mobile_device(self, payload: dict[str, Any]) -> dict[str, Any]:
        return await self._request("PUT", "mobile-devices", json=payload)

    async def delete_mobile_device(self, device_id: str, user_id: str) -> None:
        await self._request(
            "DELETE",
            f"mobile-devices/{quote(device_id, safe='')}",
            params={"user_id": user_id},
        )

    async def claim_push(self) -> dict[str, Any] | None:
        result = await self._request("POST", "push-deliveries/claim")
        return result["delivery"]

    # 할당받은 임대 ID를 함께 보내 재할당된 발송 작업에 이전 결과가 적용되지 않게 한다.
    async def complete_push(
        self, delivery: dict[str, Any], outcome: str, error_code: str | None = None
    ) -> None:
        await self._request(
            "POST",
            f"push-deliveries/{delivery['id']}/complete",
            json={
                "lease_id": delivery["lease_id"],
                "outcome": outcome,
                "error_code": error_code,
            },
        )

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

    # 이전 jti를 새 토큰 기록에 포함해 Data가 발급과 기존 토큰 폐기를 원자적으로 처리하게 한다.
    async def rotate_refresh_token(self, old_jti: str, payload: dict[str, Any]) -> Any:
        rotation_payload = dict(payload)
        rotation_payload["rotated_from_jti"] = old_jti
        return await self._request("POST", "tokens/refresh", json=rotation_payload)

    async def get_refresh_token(self, jti: str) -> dict[str, Any]:
        return await self._request("GET", f"tokens/refresh/{quote(jti, safe='')}")

    async def revoke_refresh_token(self, jti: str) -> None:
        await self._request("DELETE", f"tokens/refresh/{quote(jti, safe='')}")

    # 폐기 기록의 404는 미폐기로 해석하고 기록이 있으면 기본적으로 폐기된 토큰으로 판단한다.
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

    # 아직 DB 송출 인증값이 없는 구형 카메라는 404 대신 None으로 표현한다.
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

    async def get_live_objects(self, camera_id: str) -> dict[str, Any]:
        return await self._request(
            "GET", f"cameras/{quote(camera_id, safe='')}/objects"
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

    # 외부 검색의 start·end를 내부 계약의 from·to 질의 이름으로 바꾼다.
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

    # 전체 파일을 메모리에 읽지 않고 응답 스트림을 열며 416도 범위 요청의 정상 응답으로 중계한다.
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

    # 내부 이벤트 검색에 필요한 필터만 골라 전달하고 None 조건은 공통 요청 계층에서 뺀다.
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
