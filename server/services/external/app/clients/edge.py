# Edge 상태·이벤트 조회와 영상 설정 요청을 보내고 통신 실패를 API용 오류로 바꾼다.

from __future__ import annotations

from typing import Any

import httpx


class EdgeControlError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 502,
        details: dict[str, Any] | None = None,
        profile_outcome_journaled: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}
        # ProfileManager는 응답 전에 Edge 일지에 기록한다. 이 표시는 중앙의 중복 이벤트를 막고,
        # 그 전에 발생한 통신·사전 점검 실패는 중앙에서 기록하도록 구분한다.
        self.profile_outcome_journaled = profile_outcome_journaled


# 프록시 환경값·리다이렉트를 사용하지 않는 인증된 Edge 제어 HTTP 클라이언트이다.
class EdgeHttpClient:
    def __init__(
        self,
        *,
        base_url: str,
        auth_token: str,
        timeout_seconds: float = 5.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            headers={
                "Authorization": f"Bearer {auth_token}",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "EdgeHttpClient":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        await self.close()

    # 시간 초과·연결 불가·인증 거절·기능 거절을 구분하고 JSON 객체 응답만 받아들인다.
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(
                method, path.lstrip("/"), params=params, json=json
            )
        except httpx.TimeoutException as exc:
            raise EdgeControlError(
                "CONTROL_TIMEOUT",
                "The Edge device did not respond before the control timeout.",
                status_code=504,
            ) from exc
        except httpx.RequestError as exc:
            raise EdgeControlError(
                "EDGE_OFFLINE",
                "The Edge device is unreachable.",
                status_code=503,
            ) from exc

        payload: Any = None
        if response.content:
            try:
                payload = response.json()
            except ValueError as exc:
                raise EdgeControlError(
                    "INVALID_EDGE_RESPONSE",
                    "The Edge device returned invalid JSON.",
                ) from exc

        if response.status_code in {401, 403}:
            raise EdgeControlError(
                "EDGE_AUTH_FAILED",
                "The Edge device rejected its control credential.",
                status_code=502,
            )
        if response.status_code == 408 or response.status_code == 504:
            if isinstance(payload, dict) and payload.get("reason_code"):
                raise EdgeControlError(
                    str(payload["reason_code"]),
                    str(
                        payload.get("message")
                        or "The Edge device did not respond before the control timeout."
                    ),
                    status_code=504,
                    details=payload,
                )
            raise EdgeControlError(
                "CONTROL_TIMEOUT",
                "The Edge device did not respond before the control timeout.",
                status_code=504,
            )
        if response.status_code >= 500:
            if isinstance(payload, dict) and payload.get("reason_code"):
                raise EdgeControlError(
                    str(payload["reason_code"]),
                    str(
                        payload.get("message")
                        or "The Edge device could not complete the request."
                    ),
                    status_code=502,
                    details=payload,
                )
            raise EdgeControlError(
                "EDGE_OFFLINE",
                "The Edge control service is unavailable.",
                status_code=503,
            )
        if response.status_code >= 400:
            code = "EDGE_CONTROL_REJECTED"
            message = "The Edge device rejected the request."
            details: dict[str, Any] = {}
            if isinstance(payload, dict):
                code = str(payload.get("reason_code") or payload.get("code") or code)
                message = str(
                    payload.get("message") or payload.get("detail") or message
                )
                details = payload
            raise EdgeControlError(code, message, status_code=409, details=details)
        if not isinstance(payload, dict):
            raise EdgeControlError(
                "INVALID_EDGE_RESPONSE", "The Edge device returned an invalid response."
            )
        return payload

    async def get_status(self) -> dict[str, Any]:
        return await self._request("GET", "internal/v1/status")

    async def get_video_capabilities(self) -> dict[str, Any]:
        return await self._request("GET", "internal/v1/capabilities/video")

    # Edge가 applied를 명시해야 성공으로 보고 이미 일지에 기록된 실패인지도 표시한다.
    async def apply_video_profile(self, profile: str) -> dict[str, Any]:
        try:
            payload = await self._request(
                "PUT", "internal/v1/config/video-profile", json={"profile": profile}
            )
        except EdgeControlError as exc:
            if exc.details.get("status") == "rejected":
                exc.profile_outcome_journaled = True
            raise
        if payload.get("status") == "rejected":
            raise EdgeControlError(
                str(payload.get("reason_code") or "EDGE_CONTROL_REJECTED"),
                str(payload.get("message") or "The Edge device rejected the profile."),
                status_code=409,
                details=payload,
                profile_outcome_journaled=True,
            )
        if payload.get("status") != "applied":
            raise EdgeControlError(
                "INVALID_EDGE_RESPONSE",
                "The Edge device did not confirm profile application.",
            )
        return payload

    # 마지막 커서 이후의 이벤트 페이지를 요청하며 최초 조회에서는 after를 생략한다.
    async def list_events(
        self, *, after: str | None, limit: int = 100
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if after is not None:
            params["after"] = after
        return await self._request("GET", "internal/v1/events", params=params)
