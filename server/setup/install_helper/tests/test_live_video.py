"""Run with Python 3.11 + the desktop dependencies; FFmpeg enables the HLS test."""

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from queue import Empty, Queue
from unittest.mock import Mock, patch

import av
from PyQt5.QtCore import Qt

from server.setup.install_helper.desktop import video_worker as video
from server.setup.install_helper.desktop.api import DesktopApi
from server.setup.install_helper.desktop.local_camera import LocalCameraPublisher
from server.setup.install_helper.desktop.media import MediaBridge, hls_resource_type


class Clock:
    def __init__(self):
        self.now = 0.0
        self.stopped = False
        self.on_wait = None

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, delay):
        if self.stopped:
            return True
        self.now += delay
        if self.on_wait:
            self.on_wait()
        return self.stopped


class PlaybackTests(unittest.TestCase):
    def setUp(self):
        api = Mock()
        api.camera.return_value = {"hls_url": "/hls/cam-001/index.m3u8"}
        self.worker = video.VideoWorker((api, "cam-001"))
        self.clock = Clock()
        self.worker._stop = self.clock
        self.shown = []
        self.errors = []
        self.worker.event_ready.connect(self.errors.append, Qt.DirectConnection)
        self.worker.event_ready.connect(lambda _event: self.worker.stop(), Qt.DirectConnection)

    def play(self, samples, on_frame=None):
        samples = iter(samples)
        frames = Mock()
        frames.qsize.return_value = 0

        def get(**_kwargs):
            try:
                image, pts, session, age = next(samples)
            except StopIteration:
                self.worker.stop()
                raise Empty
            return image, pts, session, self.clock.now - age

        def show(image):
            self.shown.append((image, self.clock.now))
            self.worker.acknowledge_frame()
            if on_frame:
                on_frame(image)

        frames.get.side_effect = get
        self.worker.frame_ready.connect(show, Qt.DirectConnection)
        with patch.object(video, "Queue", return_value=frames), \
                patch.object(video.threading, "Thread"), \
                patch.object(video.time, "monotonic", side_effect=lambda: self.clock.now):
            self.worker.run()
        self.assertEqual(self.errors, [])

    def test_normal_frames_keep_source_cadence(self):
        self.play([(i, i / 30, 1, 0) for i in range(31)])
        self.assertEqual(len(self.shown), 31)
        self.assertAlmostEqual(self.shown[-1][1], 1.0)

    def test_skipped_frames_do_not_create_a_long_wait(self):
        self.play([("first", 0, 1, 0), ("latest", 8, 1, 0)])
        self.assertEqual(self.shown, [("first", 0), ("latest", 0)])

    def test_reconnect_resets_clock_even_with_a_small_pts_jump(self):
        self.play([("before", 0, 1, 0), ("after", 0.4, 2, 0)])
        self.assertEqual(self.shown, [("before", 0), ("after", 0)])

    def test_backward_and_missing_timestamps_do_not_stall(self):
        self.play([(0, 10, 1, 0), (1, 9, 1, 0), (2, None, 1, 0), (3, 20, 1, 0)])
        self.assertEqual([when for _, when in self.shown], [0, 0, 0, 0])

    def test_expired_frames_are_not_displayed(self):
        self.play([("stale", 0, 1, 2), ("fresh", 0.1, 1, 0)])
        self.assertEqual(self.shown, [("fresh", 0)])
        self.assertEqual(self.worker._dropped_frames, 1)

    def test_ui_stall_is_followed_by_a_fresh_frame(self):
        def on_frame(image):
            if image == "before":
                self.worker._frame_pending.set()
                self.clock.on_wait = resume

        def resume():
            self.clock.now = 3
            self.clock.on_wait = None
            self.worker.acknowledge_frame()

        self.play([("before", 0, 1, 0), ("old", 0.1, 1, 3),
                   ("latest", 3, 1, 0)], on_frame)
        self.assertEqual(self.shown, [("before", 0), ("latest", 3)])

    def test_decoder_does_not_wait_for_a_slow_display(self):
        frames = Queue(maxsize=video.LIVE_FRAME_QUEUE_SIZE)
        rgb = av.VideoFrame(16, 16, "rgb24")

        def decoded_frames(**_kwargs):
            for i in range(90):
                yield Mock(time=i / 30, reformat=Mock(return_value=rgb))
            self.worker.stop()

        stream = Mock()
        stream.decode.side_effect = decoded_frames
        opened = Mock()
        opened.__enter__ = Mock(return_value=stream)
        opened.__exit__ = Mock(return_value=False)
        with patch.object(video, "MediaBridge"), patch.object(av, "open", return_value=opened):
            self.worker._decode_loop(frames)
        self.assertEqual(self.errors, [])
        self.assertEqual(frames.qsize(), 30)
        self.assertAlmostEqual(frames.get_nowait()[1], 2.0)
        self.assertEqual(self.worker._dropped_frames, 60)
        self.assertEqual(self.clock.now, 0)  # No producer backpressure waits.

    def test_local_camera_requests_one_second_keyframes(self):
        publisher = LocalCameraPublisher()
        publisher.process = Mock()
        publisher.process.state.return_value = 0
        with patch.object(publisher, "ffmpeg_path", return_value="ffmpeg"):
            publisher.start("test", "rtsp://localhost:8554/test", fps=30)
        args = publisher.process.start.call_args.args[1]
        self.assertEqual(args[args.index("-g") + 1], "30")
        self.assertEqual(args[args.index("-keyint_min") + 1], "30")


class StreamDiagnosticsTests(unittest.TestCase):
    def test_rtsp_failure_is_logged_before_hls_fallback(self):
        api = Mock()
        api.camera.side_effect = [
            {"url": "rtsp://user:password@example.test:8554/cam-001"},
            {"hls_url": "/hls/cam-001/index.m3u8"},
        ]
        av_module = Mock()
        hls_stream = Mock()
        hls_open = Mock()
        hls_open.__enter__.return_value = hls_stream
        hls_open.__exit__.return_value = False
        av_module.open.side_effect = [
            OSError("connect failed rtsp://user:password@example.test:8554/cam-001"),
            hls_open,
        ]
        bridge = Mock()
        bridge.__enter__.return_value.url = "http://127.0.0.1/hls"
        bridge.__exit__.return_value = False
        with patch.object(video, "MediaBridge", return_value=bridge), self.assertLogs(
            video.LOGGER, level="WARNING"
        ) as logs:
            with video._open_live_stream(api, "cam-001", av_module):
                pass
        message = "\n".join(logs.output)
        self.assertIn("camera=cam-001", message)
        self.assertIn("OSError", message)
        self.assertIn("falling back", message)
        self.assertNotIn("password", message)

    def test_stream_error_redacts_credentials_for_supported_url_schemes(self):
        for scheme in ("http", "https", "rtsp"):
            value = video._safe_stream_error(
                RuntimeError(f"{scheme}://user:secret-{scheme}@host/path")
            )
            self.assertNotIn("secret-", value)
            self.assertIn(f"{scheme}://<redacted>@host/path", value)

        value = video._safe_stream_error(RuntimeError("Bearer secret-token"))
        self.assertNotIn("secret-token", value)
        self.assertIn("Bearer <redacted>", value)

    def test_hls_resource_types_are_classified(self):
        self.assertEqual(hls_resource_type("/hls/cam/index.m3u8"), "playlist")
        self.assertEqual(hls_resource_type("/hls/cam/segment-1.m4s"), "segment")
        self.assertEqual(hls_resource_type("/hls/cam/init.mp4"), "initialization segment")
        self.assertEqual(hls_resource_type("/hls/cam/unknown"), "other")


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is needed to generate HLS")
class HlsTests(unittest.TestCase):
    def test_real_decoder_starts_at_latest_segment_through_media_bridge(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            playlist = root / "index.m3u8"
            subprocess.run([
                shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "testsrc2=size=160x90:rate=30", "-t", "14",
                "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
                "-g", "60", "-keyint_min", "60", "-sc_threshold", "0", "-bf", "0",
                "-f", "hls", "-hls_time", "2", "-hls_list_size", "7",
                "-hls_segment_type", "fmp4", "-hls_flags", "omit_endlist",
                str(playlist),
            ], check=True, capture_output=True, timeout=30, cwd=root)
            api = DesktopApi("http://127.0.0.1")
            api.media = lambda camera, path: (root / Path(path).name).read_bytes()
            with MediaBridge(api, "cam-001", "/hls/cam-001/index.m3u8") as bridge:
                # Both opens use the same stream; only the live start changes.
                old_options = {**video.LIVE_OPEN_OPTIONS, "live_start_index": "-3"}
                with av.open(bridge.url, options=old_options, timeout=(3, 3)) as stream:
                    old_pts = next(stream.decode(video=0)).time
                with av.open(bridge.url, options=video.LIVE_OPEN_OPTIONS, timeout=(3, 3)) as stream:
                    new_pts = next(stream.decode(video=0)).time
            self.assertAlmostEqual(new_pts - old_pts, 4.0, places=2)
            self.assertAlmostEqual(new_pts, 12.0, places=2)


if __name__ == "__main__":
    unittest.main()
