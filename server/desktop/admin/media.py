"""Private loopback HLS bridge: PyAV never receives server credentials or remote URLs."""

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
