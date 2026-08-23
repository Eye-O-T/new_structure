"""Authenticated HTTP client for Edge status, event and video control APIs."""

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
        # ProfileManager persists an Edge journal entry before returning an
        # applied/rejected outcome.  Callers use this marker to avoid writing a
        # second central event for the same operation while still auditing
        # transport and preflight failures that never reached ProfileManager.
        self.profile_outcome_journaled = profile_outcome_journaled


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
                message = str(payload.get("message") or payload.get("detail") or message)
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

    async def list_events(
        self, *, after: str | None, limit: int = 100
    ) -> dict[str, Any]:
        params: dict[str, Any] = {"limit": limit}
        if after is not None:
            params["after"] = after
        return await self._request("GET", "internal/v1/events", params=params)
