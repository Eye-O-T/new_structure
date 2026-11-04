import unittest

from server.operations.doctor import _preprocessing_checks


class DoctorGpuStatusTests(unittest.TestCase):
    def test_no_workers_does_not_report_detection_gpu_as_verified(self):
        checks = _preprocessing_checks(
            {
                "status": "idle",
                "data_ready": True,
                "inference_enabled": True,
                "gpu_requested": True,
                "worker_count": 0,
                "workers": {},
            }
        )
        gpu = next(item for item in checks if item.name == "Preprocessing GPU")
        self.assertEqual(gpu.status, "WARN")
        self.assertIn("unverified", gpu.message)

    def test_ready_cuda_worker_is_reported_as_using_gpu(self):
        checks = _preprocessing_checks(
            {
                "status": "ready",
                "data_ready": True,
                "inference_enabled": True,
                "gpu_requested": True,
                "worker_count": 1,
                "workers": {
                    "cam_001": {"model_ready": True, "execution_device": "cuda:0"}
                },
            }
        )
        gpu = next(item for item in checks if item.name == "Preprocessing GPU")
        self.assertEqual(gpu.status, "OK")
        self.assertIn("using CUDA", gpu.message)


if __name__ == "__main__":
    unittest.main()
