import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from ai_cctv_edge.config import EdgeConfig
from ai_cctv_edge.pipeline import (
    build_gstreamer_command,
    build_gstreamer_description,
    redacted_command,
)


class GStreamerDescriptionTests(unittest.TestCase):
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

    def test_link_operators_remain_gstreamer_syntax(self):
        command = build_gstreamer_command(self.config, "20261111T000000.000000Z")

        description = build_gstreamer_description(command)

        self.assertIn("libcamerasrc ! video/x-raw", description)
        self.assertEqual(description.count("!"), command.count("!"))
        self.assertNotIn("'!'", description)
        self.assertNotIn("-e", description.split())

    def test_recording_location_quotes_and_escapes_path(self):
        command = build_gstreamer_command(self.config, "20261111T000000.000000Z")
        value = '/var/lib/space ! "quote" \\ folder/cam.ts'
        location_index = next(
            index
            for index, item in enumerate(command)
            if item.startswith("location=")
        )
        command[location_index] = f"location={value}"

        description = build_gstreamer_description(command)

        escaped_path = value.replace("\\", "\\\\").replace('"', '\\"')
        self.assertIn(f'location="{escaped_path}"', description)
        self.assertIn("! splitmuxsink", description)

    def test_shared_memory_path_is_a_quoted_string_property(self):
        command = [
            "gst-launch-1.0",
            "-e",
            "shmsink",
            'socket-path=/run/edge share/cam-001.h264.sock',
        ]

        description = build_gstreamer_description(command)

        self.assertIn('socket-path="/run/edge share/cam-001.h264.sock"', description)

    def test_profiles_and_publish_modes_keep_pipeline_links(self):
        for profile in ("hd", "fhd"):
            for mode in ("central_publish", "central_pull"):
                with self.subTest(profile=profile, mode=mode):
                    config = replace(
                        self.config,
                        video=self.config.video.with_profile(profile),
                        rtsp=replace(self.config.rtsp, mode=mode),
                    )
                    command = build_gstreamer_command(
                        config, "20261111T000000.000000Z"
                    )

                    description = build_gstreamer_description(command)

                    self.assertEqual(description.count("!"), command.count("!"))
                    self.assertNotIn("'!'", description)
                    self.assertIn(
                        f"width={config.video.width},height={config.video.height}",
                        description,
                    )
                    self.assertIn(
                        "rtmpsink" if mode == "central_pull" else "shmsink",
                        description,
                    )

    def test_logging_keeps_rtsp_password_redacted(self):
        command = [
            "gst-launch-1.0",
            "rtspclientsink",
            "location=rtsp://user:secret@host/cam",
        ]

        self.assertIn("user:***@host", redacted_command(command))
        self.assertNotIn("secret", redacted_command(command))

    def test_synthetic_pipeline_parses_when_gstreamer_is_installed(self):
        try:
            import gi

            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
        except (ImportError, ValueError):
            self.skipTest("PyGObject/GStreamer is not installed in this environment")

        Gst.init(None)
        command = [
            "gst-launch-1.0",
            "-e",
            "videotestsrc",
            "num-buffers=1",
            "!",
            "filesink",
            'location=/tmp/edge capture ! test.ts',
        ]
        description = build_gstreamer_description(command)
        pipeline = Gst.parse_launch(description)
        self.assertIsNotNone(pipeline)
        pipeline.set_state(Gst.State.NULL)


if __name__ == "__main__":
    unittest.main()
