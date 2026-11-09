import unittest
from pathlib import Path
from unittest.mock import Mock, call, patch

from server.services.preprocessing.processors.detection.isolation import (
    DetectionTimeoutError,
    IsolatedDetector,
)


class DetectionFirstInferenceTests(unittest.TestCase):
    def detector(self, *, timeout=10, startup_timeout=30):
        context = Mock()
        context.Pipe.return_value = (Mock(), Mock())
        with patch(
            "server.services.preprocessing.processors.detection.isolation."
            "multiprocessing.get_context",
            return_value=context,
        ), patch.object(IsolatedDetector, "_exchange", return_value={"kind": "ready"}):
            detector = IsolatedDetector(
                "pkg:Tracker",
                Path("/models/test.pt"),
                0.4,
                "cpu",
                timeout_seconds=timeout,
                startup_timeout_seconds=startup_timeout,
            )
        detector.close = Mock()
        return detector

    def test_reset_does_not_consume_first_inference_initialization_budget(self):
        detector = self.detector()
        frame = object()
        with patch.object(
            detector,
            "_exchange",
            return_value={"kind": "result", "value": {"objects": []}},
        ) as exchange:
            detector.reset()
            detector.process(frame)
            detector.process(frame)
        self.assertEqual(
            exchange.call_args_list,
            [
                call(10, ("reset", None), stage="reset"),
                call(30, ("process", frame), stage="process"),
                call(10, ("process", frame), stage="process"),
            ],
        )

    def test_first_inference_never_shortens_the_configured_frame_timeout(self):
        detector = self.detector(timeout=40, startup_timeout=30)
        with patch.object(
            detector,
            "_exchange",
            return_value={"kind": "result", "value": {"objects": []}},
        ) as exchange:
            detector.process(None)
        self.assertEqual(exchange.call_args.args[0], 40)

    def test_timeout_still_closes_the_detector(self):
        detector = self.detector()
        with patch.object(
            detector, "_exchange", side_effect=DetectionTimeoutError()
        ):
            with self.assertRaises(DetectionTimeoutError):
                detector.process(None)
        detector.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
