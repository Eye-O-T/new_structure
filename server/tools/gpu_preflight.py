"""Run isolated CUDA inference without connecting to operating services.

Mount the deployed models at /models read-only and run this file in the AI image.
The optional hold keeps detection and identity resident for a concurrent PAR check.
"""

import argparse
import json
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path


def preprocessing(hold_seconds):
    import numpy as np
    import onnxruntime as ort

    from server.services.preprocessing.processors.detection.contracts import DetectionFrame
    from server.services.preprocessing.processors.detection.isolation import IsolatedDetector
    from server.services.preprocessing.processors.identity.osnet import OsNetIdentity

    session_factory = ort.InferenceSession

    def profiled_session(*args, **kwargs):
        options = ort.SessionOptions()
        options.enable_profiling = True
        options.profile_file_prefix = "/tmp/osnet-preflight"
        kwargs["sess_options"] = options
        return session_factory(*args, **kwargs)

    ort.InferenceSession = profiled_session
    identity = OsNetIdentity()
    if identity.runtime_metadata()["execution_provider"] != "CUDAExecutionProvider":
        raise RuntimeError("OSNet did not select CUDA")
    profile_path = Path(identity._session.end_profiling())
    events = json.loads(profile_path.read_text(encoding="utf-8"))
    cuda_ops = [
        event.get("args", {}).get("op_name")
        for event in events
        if event.get("args", {}).get("provider") == "CUDAExecutionProvider"
    ]
    core_ops = {"Conv", "Gemm", "MatMul"}
    cpu_core_ops = [
        event.get("args", {}).get("op_name")
        for event in events
        if event.get("args", {}).get("provider") == "CPUExecutionProvider"
        and event.get("args", {}).get("op_name") in core_ops
    ]
    if not any(name in core_ops for name in cuda_ops) or cpu_core_ops:
        raise RuntimeError("OSNet core operations are not fully assigned to CUDA")

    import torch

    if not torch.cuda.is_available() or torch.version.cuda is None:
        raise RuntimeError("CUDA is unavailable in the preprocessing image")

    started = time.monotonic()
    detector = IsolatedDetector(
        "server.services.preprocessing.processors.detection.yolo:YoloTracker",
        Path(os.getenv("MODEL_PATH", "/models/yolo11n.pt")), 0.4, "cuda:0",
        timeout_seconds=10, startup_timeout_seconds=30,
    )
    try:
        frame = DetectionFrame(
            camera_id="preflight", tracking_session_id="0" * 32,
            observed_at=datetime.now(timezone.utc),
            image=np.zeros((720, 1280, 3), dtype=np.uint8),
        )
        detector.process(frame)
        if detector.runtime.get("execution_device") != "cuda:0":
            raise RuntimeError("detection did not select CUDA")
        print(json.dumps({
            "status": "ready", "detection": detector.runtime,
            "identity": identity.runtime_metadata(), "osnet_cuda_operations": len(cuda_ops),
            "detection_start_and_inference_seconds": round(time.monotonic() - started, 3),
        }), flush=True)
        deadline = time.monotonic() + hold_seconds
        errors = []
        identity_cycles = []

        def exercise_identity():
            try:
                while time.monotonic() < deadline:
                    identity._features(np.zeros((256, 128, 3), dtype=np.uint8))
                    identity_cycles.append(1)
                    time.sleep(0.2)
            except Exception as exc:
                errors.append(exc)

        thread = threading.Thread(target=exercise_identity)
        thread.start()
        detection_cycles = 0
        try:
            while time.monotonic() < deadline and not errors:
                detector.process(frame)
                detection_cycles += 1
                time.sleep(0.2)
        finally:
            thread.join()
        if errors:
            raise RuntimeError("OSNet failed during concurrent inference") from errors[0]
        print(json.dumps({"status": "complete", "detection_cycles": detection_cycles,
                          "identity_cycles": len(identity_cycles)}), flush=True)
    finally:
        detector.close()


def analysis(hold_seconds):
    import cv2
    import numpy as np
    import torch

    from ai_cctv_core.processing.isolation import IsolatedProcessor

    if not torch.cuda.is_available() or torch.version.cuda is None:
        raise RuntimeError("CUDA is unavailable in the analysis image")
    crop = Path("/tmp/gpu-preflight.jpg")
    image = np.random.default_rng(42).integers(0, 256, (256, 128, 3), dtype=np.uint8)
    if not cv2.imwrite(str(crop), image):
        raise RuntimeError("could not write temporary JPEG")
    job = {"object_observation": {
        "tracking_session_id": "0" * 32, "bbox": [0, 0, 128, 256],
        "frame_width": 128, "frame_height": 256, "crop_path": crop.name,
    }}
    started = time.monotonic()
    worker = IsolatedProcessor(
        "server.services.analysis.processors.attributes:PersonAttributeAnalyzer",
        timeout_seconds=120, startup_timeout_seconds=30,
    )
    try:
        result = worker.process(job, crop)
        if worker.runtime.get("execution_device") != "cuda:0" or result.get("outcome") != "complete":
            raise RuntimeError("PAR did not complete on CUDA")
        print(json.dumps({"status": "ready", "analysis": worker.runtime,
                          "analysis_start_and_inference_seconds": round(time.monotonic() - started, 3)}), flush=True)
        deadline = time.monotonic() + hold_seconds
        cycles = 0
        while time.monotonic() < deadline:
            if worker.process(job, crop).get("outcome") != "complete":
                raise RuntimeError("PAR failed during concurrent inference")
            cycles += 1
            time.sleep(0.2)
        print(json.dumps({"status": "complete", "analysis_cycles": cycles}), flush=True)
    finally:
        worker.close()
        crop.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("service", choices=("preprocessing", "analysis"))
    parser.add_argument("--hold-seconds", type=int, default=0)
    args = parser.parse_args()
    if args.service == "preprocessing":
        preprocessing(args.hold_seconds)
    else:
        analysis(args.hold_seconds)
