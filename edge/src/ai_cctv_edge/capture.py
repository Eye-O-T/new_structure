"""GStreamer capture pipeline with independently switchable output branches."""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .config import EdgeConfig
from .pipeline import build_gstreamer_command, build_gstreamer_description
from .segments import write_segment_metadata


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
        self.config = config
        command = build_gstreamer_command(config, timestamp)
        description = build_gstreamer_description(command)
        self.pipeline = Gst.parse_launch(description)
        self.Gst = Gst
        self._lock = threading.Lock()
        self._frame_count = 0
        self._last_frame_count = 0
        self.returncode: int | None = None
        self._segment_starts: dict[Path, datetime] = {}
        self._utc_time_origin: datetime | None = None
        try:
            self.bus = self.pipeline.get_bus()
            self.recording_valve = self.pipeline.get_by_name("recording_valve")
            self.stream_valve = self.pipeline.get_by_name("stream_valve")
            self.recorder = self.pipeline.get_by_name("edge_recording_sink")
            encoder = self.pipeline.get_by_name("edge_frame_watchdog")
            if any(
                element is None
                for element in (
                    self.recording_valve,
                    self.stream_valve,
                    self.recorder,
                    encoder,
                )
            ):
                raise RuntimeError("GStreamer capture controls were not created")

            self.recording_valve.set_property("drop", not recording_enabled)
            self.stream_valve.set_property("drop", not stream_enabled)
            pad = encoder.get_static_pad("src")
            if pad is None:
                raise RuntimeError("GStreamer frame monitor pad was not created")
            pad.add_probe(Gst.PadProbeType.BUFFER, self._on_frame)
            result = self.pipeline.set_state(Gst.State.PLAYING)
            if result == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError("GStreamer capture pipeline could not start")
        except BaseException:
            try:
                self.pipeline.set_state(Gst.State.NULL)
            except Exception:
                pass
            raise

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
        while True:
            message = self.bus.pop_filtered(
                self.Gst.MessageType.ERROR
                | self.Gst.MessageType.EOS
                | self.Gst.MessageType.ELEMENT
            )
            if message is None:
                return None
            if message.type == self.Gst.MessageType.ELEMENT:
                self._record_fragment_message(message)
                continue
            self.returncode = 1 if message.type == self.Gst.MessageType.ERROR else 0
            self.pipeline.set_state(self.Gst.State.NULL)
            return self.returncode

    def _fragment_utc_time(self, running_time: object) -> datetime | None:
        try:
            running_ns = int(running_time)
        except (TypeError, ValueError, OverflowError):
            return None
        # Gst.CLOCK_TIME_NONE and implausible times are not valid media timestamps.
        if not 0 <= running_ns <= 7 * 24 * 60 * 60 * 1_000_000_000:
            return None
        if self._utc_time_origin is None:
            try:
                clock = self.pipeline.get_clock()
                running_now = clock.get_time() - self.pipeline.get_base_time()
                if running_now < 0:
                    return None
                self._utc_time_origin = datetime.now(UTC) - timedelta(
                    microseconds=running_now // 1000
                )
            except (AttributeError, TypeError, ValueError, OverflowError):
                return None
        return self._utc_time_origin + timedelta(microseconds=running_ns // 1000)

    def _record_fragment_message(self, message) -> None:
        structure = message.get_structure()
        if structure is None:
            return
        name = structure.get_name()
        if name not in {
            "splitmuxsink-fragment-opened",
            "splitmuxsink-fragment-closed",
        }:
            return
        location = structure.get_value("location")
        if not isinstance(location, str):
            return
        segment = Path(location)
        if segment.suffix != ".ts":
            return
        timestamp = self._fragment_utc_time(structure.get_value("running-time"))
        if timestamp is None:
            return
        if name == "splitmuxsink-fragment-opened":
            self._segment_starts[segment] = timestamp
            return
        start_time = self._segment_starts.pop(segment, None)
        if start_time is None or timestamp <= start_time:
            return
        write_segment_metadata(
            segment,
            camera_id=self.config.camera_id,
            start_time=start_time,
            end_time=timestamp,
        )

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
            self._record_fragment_message(message)
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
        try:
            self.pipeline.send_event(self.Gst.Event.new_eos())
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                message = self.bus.timed_pop_filtered(
                    100 * self.Gst.MSECOND,
                    self.Gst.MessageType.EOS
                    | self.Gst.MessageType.ERROR
                    | self.Gst.MessageType.ELEMENT,
                )
                if message is None:
                    continue
                if message.type == self.Gst.MessageType.ELEMENT:
                    self._record_fragment_message(message)
                    continue
                if message.type == self.Gst.MessageType.ERROR:
                    error, _debug = message.parse_error()
                    raise RuntimeError(f"GStreamer capture failed while stopping: {error}")
                break
        except BaseException:
            if self.returncode is None:
                self.returncode = 1
            raise
        finally:
            try:
                self.pipeline.set_state(self.Gst.State.NULL)
                self.pipeline.get_state(2 * self.Gst.SECOND)
            finally:
                if self.returncode is None:
                    self.returncode = 0

    def wait(self, timeout: float | None = None) -> int:
        deadline = None if timeout is None else time.monotonic() + timeout
        while self.poll() is None:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError("GStreamer capture is still running")
            time.sleep(0.05)
        return int(self.returncode or 0)
