"""Publish the capture shared-memory stream without exposing credentials in argv."""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import threading
from pathlib import Path

from .config import EdgeConfig
from .config import write_atomic
from .state import default_state_root, utc_timestamp

LOGGER = logging.getLogger("ai_cctv.edge.publisher")


def _write_status(camera_id: str, status: str, error: str | None = None) -> None:
    write_atomic(
        default_state_root() / "publisher-status.json",
        json.dumps(
            {
                "camera_id": camera_id,
                "pid": os.getpid(),
                "status": status,
                "updated_at": utc_timestamp(),
                "last_error": error,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
    )


def publish(config_path: str | Path) -> int:
    config = EdgeConfig.load(config_path)
    if config.rtsp.mode != "central_publish":
        raise ValueError("publisher is only valid in central_publish mode")
    password = config.rtsp.password_file.read_text(encoding="utf-8").strip()
    if not config.rtsp.username or len(password) < 16:
        raise ValueError("valid central publish credentials are required")

    try:
        import gi

        gi.require_version("Gst", "1.0")
        from gi.repository import Gst
    except (ImportError, ValueError) as exc:
        raise RuntimeError("PyGObject and GStreamer 1.0 bindings are required") from exc

    Gst.init(None)
    socket_path = f"/run/ai-cctv-edge/{config.camera_id}.h264.sock"
    location = (
        f"rtsp://{config.rtsp.central_host}:{config.rtsp.central_port}/"
        f"{config.camera_id}"
    )
    # The URI is safe to log because the credentials are applied directly to
    # element properties and never placed in a command line or URI.
    pipeline = Gst.parse_launch(
        "shmsrc name=source is-live=true do-timestamp=true "
        "! queue leaky=downstream max-size-buffers=120 "
        "! h264parse config-interval=1 "
        "! rtspclientsink name=publisher protocols=tcp latency=200"
    )
    source = pipeline.get_by_name("source")
    publisher = pipeline.get_by_name("publisher")
    source.set_property("socket-path", socket_path)
    publisher.set_property("location", location)
    publisher.set_property("user-id", config.rtsp.username)
    publisher.set_property("user-pw", password)

    stopped = threading.Event()

    def request_stop(*_: object) -> None:
        stopped.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    bus = pipeline.get_bus()
    result = 0
    try:
        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("GStreamer publisher could not enter PLAYING state")
        _change, current, _pending = pipeline.get_state(5 * Gst.SECOND)
        if current != Gst.State.PLAYING:
            raise RuntimeError("RTSP publisher did not reach PLAYING state")
        _write_status(config.camera_id, "online")
        LOGGER.info("publishing camera %s to %s", config.camera_id, location)
        while not stopped.is_set():
            message = bus.timed_pop_filtered(
                Gst.SECOND,
                Gst.MessageType.ERROR | Gst.MessageType.EOS,
            )
            if message is None:
                continue
            if message.type == Gst.MessageType.ERROR:
                error, _debug = message.parse_error()
                LOGGER.error("publisher failed: %s", error.message)
                _write_status(config.camera_id, "offline", error.message)
                result = 1
            else:
                LOGGER.warning("publisher reached end of stream")
                _write_status(config.camera_id, "offline", "end_of_stream")
                result = 1
            break
    except Exception as exc:
        _write_status(config.camera_id, "offline", type(exc).__name__)
        raise
    finally:
        pipeline.set_state(Gst.State.NULL)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ai-cctv-edge-publisher")
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s service=edge-publisher %(message)s",
    )
    return publish(args.config)


if __name__ == "__main__":
    raise SystemExit(main())
