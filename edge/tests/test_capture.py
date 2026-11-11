import tempfile
import types
import unittest
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from ai_cctv_edge.capture import GstCapture
from ai_cctv_edge.config import EdgeConfig


class _Pad:
    def __init__(self):
        self.probe = None

    def add_probe(self, _probe_type, callback):
        self.probe = callback


class _Element:
    def __init__(self):
        self.properties = {}
        self.pad = _Pad()

    def set_property(self, name, value):
        self.properties[name] = value

    def get_static_pad(self, name):
        return self.pad if name == "src" else None


class _Bus:
    def timed_pop_filtered(self, *_args):
        return types.SimpleNamespace(type=2)

    def pop_filtered(self, *_args):
        return None


class _Pipeline:
    REQUIRED_NAMES = (
        "recording_valve",
        "stream_valve",
        "edge_recording_sink",
        "edge_frame_watchdog",
    )

    def __init__(self, *, missing_name=None, start_fails=False):
        self.elements = {
            name: _Element() for name in self.REQUIRED_NAMES if name != missing_name
        }
        self.start_fails = start_fails
        self.states = []
        self.bus = _Bus()

    def get_bus(self):
        return self.bus

    def get_by_name(self, name):
        return self.elements.get(name)

    def set_state(self, state):
        self.states.append(state)
        if state == "playing" and self.start_fails:
            return "failure"
        return "success"

    def get_state(self, *_args):
        return "success", "null", None

    def get_clock(self):
        return types.SimpleNamespace(get_time=lambda: 10_000_000_000)

    def get_base_time(self):
        return 0

    def send_event(self, _event):
        return True


class GstCaptureLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = EdgeConfig.load(
            Path(__file__).parents[1] / "config" / "config.example.toml"
        )
        self.config = replace(
            self.config,
            backup=replace(self.config.backup, root=Path(self.temporary.name)),
        )

    @staticmethod
    def _fake_gst(pipelines, pipeline_factory=None):
        def parse_launch(description):
            pipeline = (
                pipeline_factory(description)
                if pipeline_factory is not None
                else _Pipeline()
            )
            pipelines.append((description, pipeline))
            return pipeline

        gst = types.SimpleNamespace(
            init=lambda *_args: None,
            parse_launch=parse_launch,
            PadProbeType=types.SimpleNamespace(BUFFER=1),
            PadProbeReturn=types.SimpleNamespace(OK=0),
            State=types.SimpleNamespace(PLAYING="playing", NULL="null"),
            StateChangeReturn=types.SimpleNamespace(FAILURE="failure"),
            MessageType=types.SimpleNamespace(ERROR=1, EOS=2, ELEMENT=4),
            Event=types.SimpleNamespace(new_eos=lambda: object()),
            SECOND=1_000_000_000,
            MSECOND=1_000_000,
        )
        gi = types.ModuleType("gi")
        gi.require_version = lambda *_args: None
        repository = types.ModuleType("gi.repository")
        repository.Gst = gst
        return {"gi": gi, "gi.repository": repository}, gst

    def test_repeated_creation_uses_stable_named_elements(self):
        pipelines = []
        modules, _gst = self._fake_gst(pipelines)

        with patch.dict("sys.modules", modules):
            for index in range(10):
                capture = GstCapture(
                    self.config,
                    f"20261111T00000{index}.000000Z",
                    recording_enabled=True,
                    stream_enabled=True,
                )
                capture.stop()

        self.assertEqual(len(pipelines), 10)
        for description, pipeline in pipelines:
            self.assertIn("name=edge_recording_sink", description)
            self.assertIn("name=edge_frame_watchdog", description)
            self.assertEqual(pipeline.states, ["playing", "null"])
            self.assertEqual(pipeline.elements["recording_valve"].properties["drop"], False)
            self.assertEqual(pipeline.elements["stream_valve"].properties["drop"], False)

    def test_missing_required_element_releases_pipeline(self):
        pipeline = _Pipeline(missing_name="edge_recording_sink")
        modules, _gst = self._fake_gst([], lambda _description: pipeline)

        with patch.dict("sys.modules", modules):
            with self.assertRaisesRegex(RuntimeError, "controls were not created"):
                GstCapture(
                    self.config,
                    "20261111T000000.000000Z",
                    recording_enabled=True,
                    stream_enabled=True,
                )

        self.assertEqual(pipeline.states, ["null"])

    def test_missing_frame_monitor_pad_releases_pipeline(self):
        pipeline = _Pipeline()
        pipeline.elements["edge_frame_watchdog"].get_static_pad = lambda _name: None
        modules, _gst = self._fake_gst([], lambda _description: pipeline)

        with patch.dict("sys.modules", modules):
            with self.assertRaisesRegex(RuntimeError, "frame monitor pad"):
                GstCapture(
                    self.config,
                    "20261111T000000.000000Z",
                    recording_enabled=True,
                    stream_enabled=True,
                )

        self.assertEqual(pipeline.states, ["null"])

    def test_playing_failure_releases_pipeline(self):
        pipeline = _Pipeline(start_fails=True)
        modules, _gst = self._fake_gst([], lambda _description: pipeline)

        with patch.dict("sys.modules", modules):
            with self.assertRaisesRegex(RuntimeError, "could not start"):
                GstCapture(
                    self.config,
                    "20261111T000000.000000Z",
                    recording_enabled=True,
                    stream_enabled=True,
                )

        self.assertEqual(pipeline.states, ["playing", "null"])

    def test_stop_releases_pipeline_when_eos_send_fails(self):
        pipeline = _Pipeline()
        modules, _gst = self._fake_gst([], lambda _description: pipeline)
        pipeline.send_event = lambda _event: (_ for _ in ()).throw(RuntimeError("EOS failed"))

        with patch.dict("sys.modules", modules):
            capture = GstCapture(
                self.config,
                "20261111T000000.000000Z",
                recording_enabled=True,
                stream_enabled=True,
            )
            with self.assertRaisesRegex(RuntimeError, "EOS failed"):
                capture.stop()

        self.assertEqual(pipeline.states, ["playing", "null"])
        self.assertEqual(capture.returncode, 1)

    def test_fragment_close_writes_actual_running_time_metadata(self):
        pipelines = []
        modules, _gst = self._fake_gst(pipelines)
        segment = Path(self.temporary.name) / "segment.ts"
        with patch.dict("sys.modules", modules):
            capture = GstCapture(
                self.config,
                "20261111T000000.000000Z",
                recording_enabled=True,
                stream_enabled=True,
            )
            for name, running_time in (
                ("splitmuxsink-fragment-opened", 1_000_000_000),
                ("splitmuxsink-fragment-closed", 3_000_000_000),
            ):
                structure = types.SimpleNamespace(
                    get_name=lambda value=name: value,
                    get_value=lambda key, location=str(segment), rt=running_time: (
                        location if key == "location" else rt
                    ),
                )
                capture._record_fragment_message(
                    types.SimpleNamespace(get_structure=lambda: structure)
                )

        from ai_cctv_edge.segments import read_segment_metadata

        metadata = read_segment_metadata(segment, camera_id=self.config.camera_id)
        self.assertIsNotNone(metadata)
        self.assertEqual(metadata["complete"], True)
        start = datetime.fromisoformat(metadata["start_time"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(metadata["end_time"].replace("Z", "+00:00"))
        self.assertEqual((end - start).total_seconds(), 2)


if __name__ == "__main__":
    unittest.main()
