import unittest
from types import SimpleNamespace
import os
from unittest.mock import patch

from server.services.preprocessing.processors.detection.device import resolve_torch_device
from server.services.preprocessing.app.settings import Settings


def fake_torch(available, count):
    return SimpleNamespace(
        cuda=SimpleNamespace(
            is_available=lambda: available,
            device_count=lambda: count,
        )
    )


class YoloDevicePolicyTests(unittest.TestCase):
    def test_gpu_mode_resolves_auto_to_required_cuda(self):
        with patch.dict(os.environ, {"PREPROCESSING_GPU": "true", "INFERENCE_DEVICE": "auto"}, clear=True):
            self.assertEqual(Settings.from_env().device, "cuda:0")
        with patch.dict(os.environ, {"PREPROCESSING_GPU": "false", "INFERENCE_DEVICE": "auto"}, clear=True):
            self.assertEqual(Settings.from_env().device, "cpu")

    def test_cuda_device_is_rejected_when_gpu_override_is_disabled(self):
        with patch.dict(os.environ, {"PREPROCESSING_GPU": "false", "INFERENCE_DEVICE": "cuda:0"}, clear=True):
            with self.assertRaisesRegex(ValueError, "PREPROCESSING_GPU"):
                Settings.from_env()

    def test_cpu_only_policy(self):
        torch = fake_torch(False, 0)
        self.assertEqual(resolve_torch_device("auto", torch), "cpu")
        self.assertEqual(resolve_torch_device("cpu", torch), "cpu")
        with self.assertRaises(ValueError):
            resolve_torch_device("cuda", torch)
        with self.assertRaises(ValueError):
            resolve_torch_device("cuda:0", torch)

    def test_available_gpu_policy(self):
        torch = fake_torch(True, 2)
        self.assertEqual(resolve_torch_device("auto", torch), "cuda:0")
        self.assertEqual(resolve_torch_device("cuda", torch), "cuda:0")
        self.assertEqual(resolve_torch_device("cuda:1", torch), "cuda:1")
        with self.assertRaises(ValueError):
            resolve_torch_device("cuda:2", torch)

    def test_auto_import_failure_falls_back_but_explicit_does_not(self):
        with patch.dict("sys.modules", {"torch": None}):
            self.assertEqual(resolve_torch_device("auto"), "cpu")
            with self.assertRaises(ValueError):
                resolve_torch_device("cuda")


if __name__ == "__main__":
    unittest.main()
