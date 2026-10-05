"""Authenticated desktop adapter; the existing public API remains compatible."""

import ssl
import threading
from urllib.error import HTTPError
from urllib.parse import quote, urlencode, urljoin, urlsplit
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

from ..server_api import ServerApiClient, ServerApiError, _NoRedirectHandler


DESKTOP_REQUEST_TIMEOUT_SECONDS = 90
MEDIA_REQUEST_TIMEOUT_SECONDS = 8


def camera_path(camera_id):
    if not camera_id or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for c in camera_id):
        raise ValueError("올바른 카메라 ID를 선택하세요.")
    return "/api/v1/cameras/" + quote(camera_id, safe="")


class DesktopApi(ServerApiClient):
    def __init__(self, base_url, *, ca_file=None, timeout=DESKTOP_REQUEST_TIMEOUT_SECONDS, opener=None, allow_insecure_http=False):
        context = ssl.create_default_context()
        if ca_file:
            context.load_verify_locations(cafile=str(ca_file))
        transport = opener or build_opener(
            ProxyHandler({}), _NoRedirectHandler(), HTTPSHandler(context=context)
        ).open
        super().__init__(base_url, timeout=timeout, opener=transport, allow_insecure_http=allow_insecure_http)
        self._lock = threading.RLock()

    def _refresh(self):
        if not self._refresh_token:
            raise ServerApiError(401, "AUTH_REQUIRED", "다시 로그인하세요.")
        try:
            result = super()._request("POST", "/api/v1/auth/refresh",
                                      {"refresh_token": self._refresh_token}, authenticated=False)
            access, refresh = result.get("access_token"), result.get("refresh_token")
            if not access or not refresh:
                raise ServerApiError(401, "AUTH_REQUIRED", "다시 로그인하세요.")
            self._access_token, self._refresh_token = access, refresh
        except Exception:
            self._access_token = self._refresh_token = None
            raise

    def _request(self, method, path, payload=None, *, authenticated=True):
        if not path.startswith("/api/v1/") or "#" in path:
            raise ValueError("Invalid API path")
        with self._lock:
            try:
                return super()._request(method, path, payload, authenticated=authenticated)
            except ServerApiError as exc:
                if exc.status_code != 401 or not authenticated:
                    raise
                self._refresh()
                return super()._request(method, path, payload, authenticated=authenticated)

    def login(self, username, password):
        with self._lock:
            result = super().login(username, password)
            try:
                self.system_status()  # require_admin is the authority, not a client role flag.
            except Exception:
                self.logout()
                raise
            return result

    def logout(self):
        with self._lock:
            try:
                if self._refresh_token:
                    # Send the access token as well as the refresh token so the
                    # server revokes the access token and its session family.
                    self._request(
                        "POST",
                        "/api/v1/auth/logout",
                        {"refresh_token": self._refresh_token},
                        authenticated=True,
                    )
            except ServerApiError:
                # Logout is best effort; local credentials must always be cleared.
                pass
            finally:
                self._access_token = self._refresh_token = None

    def system_status(self):
        return self._request("GET", "/api/v1/admin/system/status")

    def cameras(self):
        items = []
        for offset in range(0, 10000, 100):
            page = self._request("GET", f"/api/v1/cameras?limit=100&offset={offset}")
            batch = page.get("items", [])
            items.extend(batch)
            if len(batch) < 100:
                return items
        raise ValueError("카메라 목록이 너무 큽니다.")

    def register_local_camera(self, camera_id, name):
        if not camera_id or not name:
            raise ValueError("카메라 ID와 이름이 필요합니다.")
        return self._request(
            "POST", "/api/v1/cameras", {
                "camera_id": camera_id, "name": name, "enabled": True
            }
        )

    def camera(self, camera_id, suffix="", method="GET", payload=None):
        return self._request(method, camera_path(camera_id) + suffix, payload)

    def events(self, camera_id, start, cursor=None):
        query = {"camera_id": camera_id, "from": start, "order": "asc", "limit": 100}
        if cursor:
            query["cursor"] = cursor
        return self._request("GET", "/api/v1/events?" + urlencode(query))

    def event(self, event_id):
        return self._request("GET", f"/api/v1/events/{quote(str(event_id), safe='')}")

    def event_image(self, event_id, kind):
        if kind not in {"snapshot", "crop", "annotated-snapshot"}:
            raise ValueError("Invalid event image kind")
        path = f"/api/v1/events/{quote(str(event_id), safe='')}/{kind}"
        with self._lock:
            if not self._access_token:
                raise ServerApiError(401, "AUTH_REQUIRED", "Please sign in again.")
            token = self._access_token
        for attempt in range(2):
            request = Request(self.base_url + path, headers={"Authorization": "Bearer " + token})
            try:
                with self._opener(request, timeout=MEDIA_REQUEST_TIMEOUT_SECONDS) as response:
                    content_type = response.headers.get_content_type()
                    if content_type not in {"image/jpeg", "image/png", "image/webp"}:
                        raise ValueError("Unsupported event image type")
                    content = response.read(32 * 1024 * 1024 + 1)
                    if len(content) > 32 * 1024 * 1024:
                        raise ValueError("Event image exceeds size limit")
                    return content, content_type
            except HTTPError as exc:
                if exc.code != 401 or attempt:
                    raise
                with self._lock:
                    if self._access_token == token:
                        self._refresh()
                    token = self._access_token

    def recording(self, recording_id):
        return self._request("GET", f"/api/v1/recordings/{quote(str(recording_id), safe='')}")

    def recording_playback(self, recording_id):
        return self._request("GET", f"/api/v1/recordings/{quote(str(recording_id), safe='')}/playback")

    def media_path(self, camera_id, reference, base=None):
        camera_path(camera_id)
        url = urljoin(base or self.base_url + "/", reference)
        parsed, origin = urlsplit(url), urlsplit(self.base_url)
        if (parsed.scheme, parsed.hostname, parsed.port) != (origin.scheme, origin.hostname, origin.port):
            raise ValueError("영상 주소가 로그인한 서버와 다릅니다.")
        prefix = "/hls/" + camera_id + "/"
        if (not parsed.path.startswith(prefix) or parsed.username or parsed.password
                or parsed.fragment or "%" in parsed.path or "\\" in parsed.path
                or any(part in {".", ".."} for part in parsed.path.split("/"))):
            raise ValueError("Invalid media path")
        return parsed.path + ("?" + parsed.query if parsed.query else "")

    def media(self, camera_id, path):
        path = self.media_path(camera_id, path)
        with self._lock:
            if not self._access_token:
                raise ServerApiError(401, "AUTH_REQUIRED", "다시 로그인하세요.")
            token = self._access_token
        for attempt in range(2):
            request = Request(self.base_url + path, headers={"Authorization": "Bearer " + token})
            try:
                with self._opener(request, timeout=MEDIA_REQUEST_TIMEOUT_SECONDS) as response:
                    content = response.read(32 * 1024 * 1024 + 1)
                    if len(content) > 32 * 1024 * 1024:
                        raise ValueError("Video segment exceeds size limit")
                    return content
            except HTTPError as exc:
                if exc.code != 401 or attempt:
                    raise
                with self._lock:
                    if self._access_token == token:
                        self._refresh()
                    token = self._access_token


def safe_error(exc):
    if isinstance(exc, ServerApiError):
        status = f"HTTP {exc.status_code}" if exc.status_code is not None else "LOCAL"
        message = exc.message or "(empty error message)"
        details = f" details={exc.details!r}" if exc.details is not None else ""
        return f"{status} [{exc.code}]: {message}{details}"
    return str(exc) or exc.__class__.__name__
