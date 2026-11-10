import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import patch


def _import_load_runtime_session():
    try:
        from server.services.preprocessing.processors.identity.osnet import (
            _load_runtime_session,
        )

        return _load_runtime_session
    except ImportError:
        fake_np = types.ModuleType("numpy")
        fake_np.ndarray = object
        with patch.dict(
            sys.modules,
            {
                "cv2": sys.modules.get("cv2") or types.ModuleType("cv2"),
                "numpy": sys.modules.get("numpy") or fake_np,
            },
        ):
            from server.services.preprocessing.processors.identity.osnet import (
                _load_runtime_session,
            )

            return _load_runtime_session


_load_runtime_session = _import_load_runtime_session()


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
            with self.assertRaisesRegex(ValueError, "IDENTITY_DEVICE_UNAVAILABLE"):
                _load_runtime_session(b"model", "cuda:0")

    def test_cuda_session_initialization_failure_does_not_fall_back(self):
        ort = SimpleNamespace(
            get_available_providers=lambda: ["CUDAExecutionProvider", "CPUExecutionProvider"],
            InferenceSession=lambda payload, providers: (_ for _ in ()).throw(RuntimeError("failed")),
        )
        with patch.dict(sys.modules, {"onnxruntime": ort}):
            with self.assertRaisesRegex(ValueError, "IDENTITY_DEVICE_EXECUTION_FAILED"):
                _load_runtime_session(b"model", "cuda:0")

    def test_cuda_provider_reports_requested_index(self):
        session = SimpleNamespace(get_providers=lambda: ["CUDAExecutionProvider"])
        calls = []

        def create_session(payload, providers):
            calls.append(providers)
            return session

        ort = SimpleNamespace(
            get_available_providers=lambda: [
                "CUDAExecutionProvider",
                "CPUExecutionProvider",
            ],
            InferenceSession=create_session,
        )
        with patch.dict(sys.modules, {"onnxruntime": ort}):
            result = _load_runtime_session(b"model", "cuda:1")
        self.assertEqual(result[1:], ("CUDAExecutionProvider", "cuda:1"))
        self.assertEqual(calls[0][0][1]["device_id"], 1)


if __name__ == "__main__":
    unittest.main()
