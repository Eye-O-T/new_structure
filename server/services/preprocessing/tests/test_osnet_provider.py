import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch


try:
    from server.services.preprocessing.processors.identity.osnet import _load_runtime_session
except ImportError:
    _load_runtime_session = None


class OsNetProviderTests(unittest.TestCase):
    def setUp(self):
        if _load_runtime_session is None:
            self.skipTest("preprocessing runtime dependencies are not installed")

    def test_auto_selects_cpu_when_cuda_provider_is_missing(self):
        session = SimpleNamespace(get_providers=lambda: ["CPUExecutionProvider"])
        ort = SimpleNamespace(
            get_available_providers=lambda: ["CPUExecutionProvider"],
            InferenceSession=lambda payload, providers: session,
        )
        with patch.dict(sys.modules, {"onnxruntime": ort}):
            result = _load_runtime_session(b"model", "auto")
        self.assertEqual(result[1:], ("CPUExecutionProvider", "cpu"))

    def test_explicit_cuda_requires_cuda_provider(self):
        ort = SimpleNamespace(get_available_providers=lambda: ["CPUExecutionProvider"])
        with patch.dict(sys.modules, {"onnxruntime": ort}):
            with self.assertRaisesRegex(ValueError, "CUDAExecutionProvider"):
                _load_runtime_session(b"model", "cuda:0")

    def test_cuda_provider_reports_requested_index(self):
        session = SimpleNamespace(get_providers=lambda: ["CUDAExecutionProvider"])
        calls = []

        def create_session(payload, providers):
            calls.append(providers)
            return session

        ort = SimpleNamespace(
            get_available_providers=lambda: ["CUDAExecutionProvider", "CPUExecutionProvider"],
            InferenceSession=create_session,
        )
        with patch.dict(sys.modules, {"onnxruntime": ort}):
            result = _load_runtime_session(b"model", "cuda:1")
        self.assertEqual(result[1:], ("CUDAExecutionProvider", "cuda:1"))
        self.assertEqual(calls[0][0][1]["device_id"], 1)


if __name__ == "__main__":
    unittest.main()
