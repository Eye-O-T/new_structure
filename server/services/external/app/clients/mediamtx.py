from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import quote

import httpx


class MediaControlError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "MEDIA_CONTROL_UNAVAILABLE",
        status_code: int = 503,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


class MediaMtxClient:
    """Narrow MediaMTX v1.9 client used only to revoke active publishers."""

    def __init__(
        self,
        base_url: str,
        *,
        timeout_seconds: float = 5.0,
        verification_interval_seconds: float = 0.1,
        verification_quiet_checks: int = 10,
        verification_max_checks: int = 30,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if verification_interval_seconds < 0:
            raise ValueError("verification interval cannot be negative")
        if verification_quiet_checks < 2:
            raise ValueError("publisher verification requires two quiet checks")
        if verification_max_checks < verification_quiet_checks:
            raise ValueError("publisher verification check limit is too small")
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/") + "/",
            timeout=timeout_seconds,
            transport=transport,
            trust_env=False,
        )
        self._verification_interval_seconds = verification_interval_seconds
        self._verification_quiet_checks = verification_quiet_checks
        self._verification_max_checks = verification_max_checks

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str) -> httpx.Response:
        try:
            return await self._client.request(method, path)
        except httpx.RequestError as exc:
            raise MediaControlError("MediaMTX control API is unavailable.") from exc

    async def _publisher_session(self, camera_id: str) -> str | None:
        response = await self._request(
            "GET", f"v3/paths/get/{quote(camera_id, safe='')}"
        )
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise MediaControlError("MediaMTX rejected the path status request.")
        try:
            path: dict[str, Any] = response.json()
            source = path.get("source")
        except (TypeError, ValueError) as exc:
            raise MediaControlError(
                "MediaMTX returned an invalid path status response.",
                code="INVALID_MEDIA_RESPONSE",
                status_code=502,
            ) from exc
        if source is None:
            return None
        if not isinstance(source, dict):
            raise MediaControlError(
                "MediaMTX returned an invalid path source.",
                code="INVALID_MEDIA_RESPONSE",
                status_code=502,
            )
        source_type = source.get("type")
        session_id = source.get("id")
        if source_type != "rtspSession" or not isinstance(session_id, str):
            raise MediaControlError(
                "The active MediaMTX source is not a revocable RTSP publisher.",
                code="UNSUPPORTED_MEDIA_SOURCE",
                status_code=409,
            )
        return session_id

    async def _kick_publisher_session(self, session_id: str) -> bool:
        kicked = await self._request(
            "POST", f"v3/rtspsessions/kick/{quote(session_id, safe='')}"
        )
        if kicked.status_code in {200, 204}:
            return True
        if kicked.status_code == 404:
            # The publisher disconnected between the status and kick calls.
            return False
        raise MediaControlError("MediaMTX could not disconnect the RTSP publisher.")

    async def disconnect_publisher(self, camera_id: str) -> bool:
        """Kick publishers until the path stays quiet across repeated checks.

        A MediaMTX HTTP-auth request that completed just before a camera was
        disabled can attach after the first path lookup. Requiring consecutive
        quiet observations catches that late source while new auth requests are
        held behind the camera lifecycle lock and will subsequently be denied.
        """

        kicked_any = False
        quiet_checks = 0
        for check in range(self._verification_max_checks):
            session_id = await self._publisher_session(camera_id)
            if session_id is None:
                quiet_checks += 1
                if quiet_checks >= self._verification_quiet_checks:
                    return kicked_any
            else:
                quiet_checks = 0
                kicked_any = (
                    await self._kick_publisher_session(session_id) or kicked_any
                )
            if check + 1 < self._verification_max_checks:
                await asyncio.sleep(self._verification_interval_seconds)
        raise MediaControlError(
            "MediaMTX publisher did not become quiescent.",
            code="MEDIA_PUBLISHER_STILL_ACTIVE",
        )
