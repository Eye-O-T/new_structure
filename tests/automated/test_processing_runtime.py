"""실제 자식 프로세스 종료, 완료 재전송, 이미지 디코딩 경계를 검증한다."""

import asyncio
import json
import threading
import time
from pathlib import Path

import cv2
import httpx
import numpy as np
import pytest

from ai_cctv_core.processing.images import load_observation_crop
from ai_cctv_core.processing.isolation import IsolatedProcessor
from ai_cctv_core.processing.runtime import running_worker
from ai_cctv_core.processing.worker import ObjectWorker, safe_crop


class FastProcessor:
    def process(self, job, crop_path):
        return {"outcome": "complete", "metadata": {"value": job["id"]}}


class SlowOnceProcessor:
    def process(self, job, crop_path):
        marker = Path(crop_path).with_suffix(".attempted")
        if not marker.exists():
            marker.touch()
            time.sleep(60)
        return {"outcome": "complete", "metadata": {"recovered": True}}


class SlowStartupProcessor:
    def __init__(self):
        time.sleep(60)


def _job(crop="crop.jpg", width=32, height=64):
    return {
        "id": 1,
        "event_id": 2,
        "lease_id": "b" * 32,
        "camera_id": "cam-001",
        "person_id": "1",
        "object_observation": {
            "tracking_session_id": "a" * 32,
            "bbox": [0, 0, width, height],
            "frame_width": width,
            "frame_height": height,
            "crop_path": crop,
        },
    }


def _jpeg(path, width=32, height=64):
    pixels = np.full((height, width, 3), (32, 64, 192), dtype=np.uint8)
    success, encoded = cv2.imencode(".jpg", pixels)
    assert success
    path.write_bytes(encoded.tobytes())
    return pixels


def test_crop_decoder_checks_header_size_and_exact_observation(tmp_path):
    crop = tmp_path / "crop.jpg"
    pixels = _jpeg(crop)
    decoded = load_observation_crop(_job(), crop)
    assert decoded.shape == pixels.shape
    assert abs(float(decoded.mean()) - float(pixels.mean())) < 2
    with pytest.raises(ValueError):
        load_observation_crop(_job(width=31), crop)
    with pytest.raises(ValueError):
        load_observation_crop(_job(), crop, max_pixels=100)
    with pytest.raises(ValueError):
        load_observation_crop(_job(), crop, max_file_bytes=20)
    with pytest.raises(ValueError):
        load_observation_crop({}, crop)
    with pytest.raises(FileNotFoundError):
        load_observation_crop(_job(), tmp_path / "missing.jpg")


@pytest.mark.parametrize(
    "payload", [b"not a jpeg", b"\xff\xd8\xff\xda\x00\x02\xff\xd9"]
)
def test_crop_rejects_missing_frame_header_and_truncation(tmp_path, payload):
    crop = tmp_path / "crop.jpg"
    crop.write_bytes(payload)
    with pytest.raises(ValueError):
        load_observation_crop(_job(), crop)


def test_crop_rejects_huge_header_before_native_decode(tmp_path, monkeypatch):
    crop = tmp_path / "crop.jpg"
    _jpeg(crop)
    payload = bytearray(crop.read_bytes())
    marker = payload.index(b"\xff\xc0")
    payload[marker + 5 : marker + 9] = b"\x3f\xff\x3f\xff"
    crop.write_bytes(payload)

    def forbidden(*args):
        pytest.fail("Oversized input reached OpenCV")

    monkeypatch.setattr(cv2, "imdecode", forbidden)
    with pytest.raises(ValueError):
        load_observation_crop(_job(width=16383, height=16383), crop)


@pytest.mark.parametrize(
    "path", ["/absolute.jpg", "C:/crop.jpg", "C:crop.jpg", "..\\crop.jpg"]
)
def test_crop_path_rejects_absolute_and_platform_ambiguous_paths(tmp_path, path):
    with pytest.raises(ValueError):
        safe_crop(tmp_path, path)


@pytest.mark.asyncio
async def test_completion_retry_keeps_same_lease_without_rerunning_model(tmp_path):
    (tmp_path / "crop.jpg").write_bytes(b"plugin owns decoding")
    claims, calls, reports = [], [], []

    class Processor:
        def process(self, job, crop):
            calls.append(job["id"])
            return {"outcome": "complete", "metadata": {"result": 1}}

    def handler(request):
        if request.url.path.endswith("/claim"):
            claims.append(request.url.path)
            return httpx.Response(200, json={"job": _job()})
        reports.append(json.loads(request.content))
        if len(reports) == 1:
            raise httpx.ConnectError("response lost", request=request)
        return httpx.Response(200, json={"accepted": True})

    async with httpx.AsyncClient(
        base_url="http://data", transport=httpx.MockTransport(handler)
    ) as client:
        worker = ObjectWorker("analysis", client, tmp_path, Processor())
        with pytest.raises(httpx.ConnectError):
            await worker.once()
        assert await worker.once()
    assert len(claims) == 1 and calls == [1]
    assert reports[0] == reports[1]
    assert worker.last_outcome == "complete"


def test_isolated_processor_kills_timeout_and_recovers_in_new_process(tmp_path):
    processor = IsolatedProcessor(
        "tests.automated.test_processing_runtime:SlowOnceProcessor",
        timeout_seconds=3,
        startup_timeout_seconds=10,
    )
    original = processor._process
    try:
        with pytest.raises(TimeoutError):
            processor.process(_job(), tmp_path / "crop.jpg")
        assert processor._process is None
        assert processor.restartable
        # 이전 프로세스는 close()까지 끝났다. 살아 있는 모델에 다음 작업을 겹치지 않는다.
        with pytest.raises(ValueError):
            original.is_alive()
        assert processor.process(_job(), tmp_path / "crop.jpg")["metadata"]["recovered"]
    finally:
        processor.close()
    assert processor._process is None


@pytest.mark.asyncio
async def test_lost_completion_acknowledgement_does_not_leave_idle_worker_unhealthy(
    tmp_path,
):
    (tmp_path / "crop.jpg").write_bytes(b"plugin owns decoding")
    claims, reports = [], []

    def handler(request):
        if request.url.path.endswith("/claim"):
            claims.append(True)
            return httpx.Response(
                200, json={"job": _job() if len(claims) == 1 else None}
            )
        reports.append(json.loads(request.content))
        if len(reports) == 1:
            raise httpx.ReadTimeout("committed but response lost", request=request)
        return httpx.Response(200, json={"accepted": False})

    async with httpx.AsyncClient(
        base_url="http://data", transport=httpx.MockTransport(handler)
    ) as client:
        worker = ObjectWorker("analysis", client, tmp_path, FastProcessor())
        with pytest.raises(httpx.ReadTimeout):
            await worker.once()
        assert await worker.once()
        assert worker.last_outcome == "rejected"
        assert worker.last_error == "COMPLETION_NOT_ACCEPTED"
        assert not await worker.once()
    assert reports[0] == reports[1]
    assert worker.last_outcome == "rejected"
    assert worker.last_error is None and worker.ready


def test_isolated_startup_has_a_deadline():
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        IsolatedProcessor(
            "tests.automated.test_processing_runtime:SlowStartupProcessor",
            startup_timeout_seconds=0.2,
        )
    assert time.monotonic() - started < 5


def test_isolated_close_interrupts_in_flight_processing(tmp_path):
    processor = IsolatedProcessor(
        "tests.automated.test_processing_runtime:SlowOnceProcessor",
        timeout_seconds=60,
    )
    errors = []

    def process():
        try:
            processor.process(_job(), tmp_path / "crop.jpg")
        except (OSError, EOFError, RuntimeError):
            errors.append(True)

    thread = threading.Thread(target=process, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 5
        while (
            not (tmp_path / "crop.attempted").exists() and time.monotonic() < deadline
        ):
            time.sleep(0.02)
        assert (tmp_path / "crop.attempted").exists()
        processor.close()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert errors and processor._process is None
    finally:
        processor.close()


@pytest.mark.asyncio
async def test_runtime_shutdown_cancels_slow_initialization(tmp_path, monkeypatch):
    monkeypatch.setenv("OBJECT_STARTUP_TIMEOUT_SECONDS", "120")

    async def run():
        async with running_worker(
            "analysis",
            "a" * 40,
            "http://data",
            tmp_path,
            "tests.automated.test_processing_runtime:SlowStartupProcessor",
        ):
            pytest.fail("Slow initialization unexpectedly completed")

    task = asyncio.create_task(run())
    await asyncio.sleep(0.2)
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    assert time.monotonic() - started < 5


def test_compose_empty_identity_model_uses_cpu_default(tmp_path, monkeypatch):
    from server.services.preprocessing.processors.identity import (
        LocalAppearanceIdentity,
    )

    monkeypatch.setenv("IDENTITY_MODEL_PATH", "")
    crop = tmp_path / "crop.jpg"
    _jpeg(crop)
    result = LocalAppearanceIdentity().process(_job(), crop)
    assert result["identity_descriptor"]["space_id"] == "appearance-hsv-v1"


@pytest.mark.parametrize(
    "reference, result_key",
    [
        ("server.services.analysis.processors:LocalAppearanceAnalyzer", "metadata"),
        (
            "server.services.preprocessing.processors.identity:LocalAppearanceIdentity",
            "identity_descriptor",
        ),
    ],
)
def test_default_cpu_plugins_process_real_jpeg_in_spawned_child(
    tmp_path, monkeypatch, reference, result_key
):
    monkeypatch.setenv("IDENTITY_MODEL_PATH", "")
    crop = tmp_path / "crop.jpg"
    _jpeg(crop)
    processor = IsolatedProcessor(reference, timeout_seconds=10)
    try:
        result = processor.process(_job(), crop)
        assert result["outcome"] == "complete"
        assert result[result_key]
    finally:
        processor.close()
