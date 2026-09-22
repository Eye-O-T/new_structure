"""Private loopback HLS bridge: PyAV never receives server credentials or remote URLs."""

import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit


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
                try:
                    path = bridge.api.media_path(bridge.camera_id, self.path[len(bridge.prefix):])
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
                except Exception:
                    self.send_error(502, "Media unavailable")

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
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
