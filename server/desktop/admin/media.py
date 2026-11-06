"""Private loopback HLS and playback bridges: external clients never receive server credentials."""

import re
import secrets
import threading
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError
from urllib.parse import urlsplit


LOGGER = logging.getLogger(__name__)
CLIENT_DISCONNECT_ERRORS = (BrokenPipeError, ConnectionAbortedError, ConnectionResetError)


def hls_resource_type(path):
    path = urlsplit(path).path.lower()
    if path.endswith(".m3u8"):
        return "playlist"
    if path.endswith((".m4s", ".ts")):
        return "segment"
    if path.endswith((".mp4", ".aac", ".webm")):
        return "initialization segment"
    return "other"


class MediaBridge:
    def __init__(self, api, camera_id, reference):
        self.api, self.camera_id = api, camera_id
        self.prefix = "/" + secrets.token_urlsafe(32)
        initial = api.media_path(camera_id, reference)
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_GET(self):
                if not self.path.startswith(bridge.prefix + "/hls/"):
                    self.send_error(404)
                    return
                upstream_path = None
                try:
                    path = bridge.api.media_path(bridge.camera_id, self.path[len(bridge.prefix):])
                    upstream_path = path
                    content = bridge.api.media(bridge.camera_id, path)
                    playlist = urlsplit(path).path.endswith(".m3u8")
                    if playlist:
                        content = bridge.playlist(content, path)
                    self.send_response(200)
                    self.send_header("Content-Type", "application/vnd.apple.mpegurl" if playlist else "application/octet-stream")
                    self.send_header("Content-Length", str(len(content)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(content)
                except CLIENT_DISCONNECT_ERRORS:
                    LOGGER.debug(
                        "HLS client disconnected camera=%s resource=%s path=%s",
                        bridge.camera_id,
                        hls_resource_type(upstream_path or self.path),
                        upstream_path or self.path,
                    )
                    return
                except HTTPError as exc:
                    path = upstream_path or self.path
                    LOGGER.warning(
                        "HLS upstream request failed camera=%s resource=%s path=%s status=%s",
                        bridge.camera_id,
                        hls_resource_type(path),
                        path,
                        exc.code,
                    )
                    try:
                        self.send_error(502, "Media unavailable")
                    except CLIENT_DISCONNECT_ERRORS:
                        LOGGER.debug("HLS client disconnected while sending error camera=%s", bridge.camera_id)
                except Exception as exc:
                    path = upstream_path or self.path
                    LOGGER.warning(
                        "HLS media request failed camera=%s resource=%s path=%s "
                        "error_type=%s error=%s",
                        bridge.camera_id,
                        hls_resource_type(path),
                        path,
                        type(exc).__name__,
                        str(exc) or exc.__class__.__name__,
                    )
                    try:
                        self.send_error(502, "Media unavailable")
                    except CLIENT_DISCONNECT_ERRORS:
                        LOGGER.debug("HLS client disconnected while sending error camera=%s", bridge.camera_id)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1})
        self.url = f"http://127.0.0.1:{self.server.server_port}" + self.prefix + initial

    def playlist(self, content, path):
        def rewrite(reference):
            target = self.api.media_path(self.camera_id, reference, self.api.base_url + path)
            return self.prefix + target

        lines = []
        for line in content.decode("utf-8").splitlines():
            if line.startswith("#"):
                line = re.sub(r'URI="([^"]+)"', lambda m: 'URI="' + rewrite(m[1]) + '"', line)
            elif line.strip():
                line = rewrite(line.strip())
            lines.append(line)
        return ("\n".join(lines) + "\n").encode()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class PlaybackBridge:
    """Loopback authenticated proxy so an external browser can play recordings without cookies."""

    _FORWARDED_HEADERS = (
        "Content-Type",
        "Content-Length",
        "Content-Range",
        "Accept-Ranges",
        "Cache-Control",
        "ETag",
        "Last-Modified",
    )

    def __init__(self, api, reference):
        self.api = api
        self.upstream_path = api.playback_path(reference)
        self.local_path = "/" + secrets.token_urlsafe(32)
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _handle_playback(self, method):
                req_path = urlsplit(self.path).path
                if req_path != bridge.local_path:
                    self.send_error(404)
                    return
                try:
                    with bridge.api.open_playback(
                        bridge.upstream_path,
                        method=method,
                        range_header=self.headers.get("Range"),
                        if_range_header=self.headers.get("If-Range"),
                    ) as upstream:
                        status = getattr(upstream, "status", None) or getattr(
                            upstream, "code", 200
                        )
                        self.send_response(status)
                        headers = getattr(upstream, "headers", None) or {}
                        sent_content_type = False
                        for name in bridge._FORWARDED_HEADERS:
                            value = headers.get(name)
                            if value:
                                self.send_header(name, value)
                                if name.lower() == "content-type":
                                    sent_content_type = True
                        if not sent_content_type:
                            self.send_header("Content-Type", "video/mp4")
                        self.end_headers()
                        if method != "HEAD":
                            while True:
                                chunk = upstream.read(64 * 1024)
                                if not chunk:
                                    break
                                self.wfile.write(chunk)
                except CLIENT_DISCONNECT_ERRORS:
                    LOGGER.debug(
                        "Playback client disconnected path=%s", bridge.upstream_path
                    )
                except HTTPError as exc:
                    try:
                        self.send_response(exc.code)
                        headers = getattr(exc, "headers", None) or {}
                        for name in ("Content-Range", "Content-Type"):
                            value = headers.get(name)
                            if value:
                                self.send_header(name, value)
                        self.end_headers()
                    except CLIENT_DISCONNECT_ERRORS:
                        LOGGER.debug(
                            "Playback client disconnected while sending HTTP error path=%s",
                            bridge.upstream_path,
                        )
                except Exception as exc:
                    LOGGER.warning(
                        "Playback media request failed path=%s error_type=%s error=%s",
                        bridge.upstream_path,
                        type(exc).__name__,
                        str(exc) or exc.__class__.__name__,
                    )
                    try:
                        self.send_error(502, "Media unavailable")
                    except CLIENT_DISCONNECT_ERRORS:
                        LOGGER.debug(
                            "Playback client disconnected while sending error path=%s",
                            bridge.upstream_path,
                        )

            def do_GET(self):
                self._handle_playback("GET")

            def do_HEAD(self):
                self._handle_playback("HEAD")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            kwargs={"poll_interval": 0.1},
            daemon=True,
        )
        self.url = f"http://127.0.0.1:{self.server.server_port}{self.local_path}"
        self._closed = False

    def start(self):
        self.thread.start()
        return self

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.thread.is_alive():
            self.server.shutdown()
            self.thread.join(timeout=2)
        self.server.server_close()

    def __enter__(self):
        return self.start()

    def __exit__(self, *_args):
        self.close()
