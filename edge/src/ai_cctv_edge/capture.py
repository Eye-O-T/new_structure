"""GStreamer capture pipeline with independently switchable output branches."""

from __future__ import annotations

import shlex
import threading
import time

from .config import EdgeConfig
from .pipeline import build_gstreamer_command


class GstCapture:
    def __init__(
        self,
        config: EdgeConfig,
        timestamp: str,
        *,
        recording_enabled: bool,
        stream_enabled: bool,
    ):
        try:
            import gi

            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
        except (ImportError, ValueError) as exc:
            raise RuntimeError("PyGObject and GStreamer 1.0 bindings are required") from exc

        Gst.init(None)
        command = build_gstreamer_command(config, timestamp)
        description = shlex.join(part for part in command[1:] if part != "-e")
        self.pipeline = Gst.parse_launch(description)
        self.Gst = Gst
        self._lock = threading.Lock()
        self._frame_count = 0
        self._last_frame_count = 0
        self.returncode: int | None = None
        self.bus = self.pipeline.get_bus()
        self.recording_valve = self.pipeline.get_by_name("recording_valve")
        self.stream_valve = self.pipeline.get_by_name("stream_valve")
        self.recorder = self.pipeline.get_by_name("splitmuxsink0")
        if self.recording_valve is None or self.stream_valve is None or self.recorder is None:
            raise RuntimeError("GStreamer capture output controls were not created")
        self.recording_valve.set_property("drop", not recording_enabled)
        self.stream_valve.set_property("drop", not stream_enabled)
        encoder = self.pipeline.get_by_name("watchdog0")
        if encoder is None:
            encoder = self.pipeline.get_by_name("videoconvert0")
        pad = encoder.get_static_pad("src") if encoder is not None else None
        if pad is not None:
            pad.add_probe(Gst.PadProbeType.BUFFER, self._on_frame)
        result = self.pipeline.set_state(Gst.State.PLAYING)
        if result == Gst.StateChangeReturn.FAILURE:
            self.pipeline.set_state(Gst.State.NULL)
            raise RuntimeError("GStreamer capture pipeline could not start")

    def _on_frame(self, _pad, _info):
        with self._lock:
            self._frame_count += 1
        return self.Gst.PadProbeReturn.OK

    def frame_activity(self) -> bool:
        with self._lock:
            current = self._frame_count
        changed = current != self._last_frame_count
        self._last_frame_count = current
        return changed

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        message = self.bus.pop_filtered(
            self.Gst.MessageType.ERROR | self.Gst.MessageType.EOS
        )
        if message is None:
            return None
        self.returncode = 1 if message.type == self.Gst.MessageType.ERROR else 0
        self.pipeline.set_state(self.Gst.State.NULL)
        return self.returncode

    def set_recording_enabled(self, enabled: bool) -> None:
        enabled = bool(enabled)
        was_dropping = bool(self.recording_valve.get_property("drop"))
        if enabled:
            self.recording_valve.set_property("drop", False)
            return
        if was_dropping:
            return
        self.recorder.emit("split-now")
        deadline = time.monotonic() + 3.0
        fragment_closed = False
        while time.monotonic() < deadline:
            message = self.bus.timed_pop_filtered(
                100 * self.Gst.MSECOND,
                self.Gst.MessageType.ELEMENT
                | self.Gst.MessageType.ERROR
                | self.Gst.MessageType.EOS,
            )
            if message is None:
                continue
            if message.type == self.Gst.MessageType.ERROR:
                self.recording_valve.set_property("drop", True)
                error, _debug = message.parse_error()
                raise RuntimeError(f"GStreamer recording branch failed: {error}")
            structure = message.get_structure()
            if structure is not None and structure.get_name() == "splitmuxsink-fragment-closed":
                fragment_closed = True
                break
        self.recording_valve.set_property("drop", True)
        if not fragment_closed:
            raise RuntimeError("GStreamer recording segment did not close")

    def set_stream_enabled(self, enabled: bool) -> None:
        self.stream_valve.set_property("drop", not bool(enabled))

    def stop(self) -> None:
        self.pipeline.send_event(self.Gst.Event.new_eos())
        self.bus.timed_pop_filtered(
            2 * self.Gst.SECOND,
            self.Gst.MessageType.EOS | self.Gst.MessageType.ERROR,
        )
        self.pipeline.set_state(self.Gst.State.NULL)
        self.pipeline.get_state(2 * self.Gst.SECOND)
        if self.returncode is None:
            self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("GStreamer capture is still running")
            time.sleep(0.05)
        return int(self.returncode or 0)
