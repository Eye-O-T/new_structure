"""OSNet 실행 계약을 DNN 대역으로 검증한다. 학습 가중치의 실제 정확도 검증은 별개다."""

import hashlib
import json

import cv2
import httpx
import numpy as np
import pytest

from ai_cctv_core.contracts.objects import ObjectJobCompletion
from ai_cctv_core.processing.worker import ObjectWorker
from server.services.preprocessing.processors.identity import OsNetIdentity
from server.services.preprocessing.processors.identity import appearance
from server.services.preprocessing.processors.identity.osnet import (
    DEFAULT_MODEL_FILENAME,
)
from server.services.preprocessing.tests.test_local_identity import _Net, _crop, _person


@pytest.fixture(autouse=True)
def no_deployment_model(monkeypatch):
    monkeypatch.delenv("IDENTITY_MODEL_PATH", raising=False)


class _OsNet(_Net):
    """실제 모델을 가장하지 않으며 입력 버퍼와 시험·실제 추론 호출 횟수만 검사한다."""

    def __init__(self, output=None, outputs=("embedding",)):
        super().__init__(
            output
            if output is not None
            else np.arange(-256, 256, dtype=np.float32)[None],
            outputs,
        )
        self.calls = 0
        self.fail = False

    def setInput(self, value):
        assert value.shape == (1, 3, 256, 128)
        assert value.dtype == np.float32
        assert value.flags.c_contiguous
        super().setInput(value)

    def forward(self):
        self.calls += 1
        if self.fail:
            raise cv2.error("incompatible graph input")
        return super().forward()


def _model(tmp_path, monkeypatch, network=None):
    root = tmp_path / "models"
    root.mkdir(exist_ok=True)
    model = root / DEFAULT_MODEL_FILENAME
    payload = b"DNN contract test fixture, not trained OSNet weights"
    model.write_bytes(payload)
    network = network if network is not None else _OsNet()

    def load(buffer):
        assert buffer.tobytes() == payload
        return network

    monkeypatch.setattr(cv2.dnn, "readNetFromONNX", load)
    return model, root, payload, network


def test_osnet_requires_a_model_and_never_selects_hsv(tmp_path, monkeypatch):
    def forbidden(*_, **__):
        pytest.fail("OSNet must not invoke handcrafted appearance features")

    monkeypatch.setattr(appearance, "_handcrafted_features", forbidden)
    with pytest.raises(FileNotFoundError, match=DEFAULT_MODEL_FILENAME):
        OsNetIdentity(models_root=tmp_path)
    monkeypatch.setenv("IDENTITY_MODEL_PATH", "")
    with pytest.raises(FileNotFoundError, match=DEFAULT_MODEL_FILENAME):
        OsNetIdentity(models_root=tmp_path)
    with pytest.raises(ValueError, match="must name"):
        OsNetIdentity("", models_root=tmp_path)


@pytest.mark.parametrize("selection", ["explicit", "environment", "default"])
def test_osnet_loads_selected_bytes_and_checks_inference_before_ready(
    tmp_path, monkeypatch, selection
):
    model, root, payload, network = _model(tmp_path, monkeypatch)
    if selection == "environment":
        monkeypatch.setenv("IDENTITY_MODEL_PATH", str(model))
    plugin = OsNetIdentity(model if selection == "explicit" else None, models_root=root)
    assert network.calls == 1
    job, crop = _crop(tmp_path, _person())
    result = plugin.process(job, crop)
    assert network.calls == 2
    digest = hashlib.sha256(payload).hexdigest()
    descriptor = result["identity_descriptor"]
    assert descriptor["space_id"] == f"osnet:{digest}:rgb256x128-imagenet-v1"
    assert len(descriptor["space_id"]) <= 128
    assert descriptor["schema_version"] == 1
    vector = np.asarray(descriptor["features"])
    assert vector.shape == (512,)
    assert np.isfinite(vector).all()
    assert np.linalg.norm(vector) == pytest.approx(1.0, abs=1e-12)
    assert np.any(vector < 0)
    metadata = result["metadata"]
    assert metadata["backend"] == "osnet"
    assert metadata["architecture"] == "osnet_x0_25"
    assert metadata["model_sha256"] == digest
    assert metadata["quality"]["crop_width"] == 120
    assert metadata["quality"]["crop_height"] == 240
    assert "features" not in json.dumps(metadata)
    assert "global_person_id" not in result
    assert model.read_bytes() == payload
    assert (
        ObjectJobCompletion(lease_id="a" * 32, **result).identity_descriptor is not None
    )


def test_osnet_actual_crop_uses_exact_rgb_imagenet_input(tmp_path, monkeypatch):
    model, root, _, network = _model(tmp_path, monkeypatch)
    plugin = OsNetIdentity(model, models_root=root)
    image = np.full((256, 128, 3), (10, 80, 210), dtype=np.uint8)
    image[200:] = (20, 40, 60)
    job, crop = _crop(tmp_path, image)
    plugin.process(job, crop)
    decoded = cv2.imread(str(crop))
    rgb = decoded[100, 60, ::-1].astype(np.float32) / 255
    expected = (rgb - np.array([0.485, 0.456, 0.406])) / np.array([0.229, 0.224, 0.225])
    np.testing.assert_allclose(network.input[0, :, 100, 60], expected, atol=1e-6)


@pytest.mark.parametrize(
    "output",
    [
        np.ones((1, 511), dtype=np.float32),
        np.ones((1, 1000), dtype=np.float32),
        np.ones((2, 512), dtype=np.float32),
        np.ones((512,), dtype=np.float32),
        np.ones((1, 512, 1, 1), dtype=np.float32),
        np.ones((1, 512), dtype=np.float64),
        np.ones((1, 512), dtype=np.float16),
        np.ones((1, 512), dtype=np.int32),
        np.zeros((1, 512), dtype=np.float32),
        np.full((1, 512), np.nan, dtype=np.float32),
        np.full((1, 512), np.inf, dtype=np.float32),
    ],
)
def test_osnet_incompatible_output_fails_at_startup(tmp_path, monkeypatch, output):
    model, root, _, _ = _model(tmp_path, monkeypatch, _OsNet(output))
    with pytest.raises(ValueError):
        OsNetIdentity(model, models_root=root)


@pytest.mark.parametrize(
    "output",
    [
        np.ones((1, 256), dtype=np.float32),
        np.zeros((1, 512), dtype=np.float32),
        np.full((1, 512), np.nan, dtype=np.float32),
    ],
)
def test_osnet_checks_every_embedding_after_startup(tmp_path, monkeypatch, output):
    model, root, _, network = _model(tmp_path, monkeypatch)
    plugin = OsNetIdentity(model, models_root=root)
    network.output = output
    job, crop = _crop(tmp_path, _person())
    with pytest.raises(ValueError):
        plugin.process(job, crop)


@pytest.mark.parametrize("during_startup", [True, False])
def test_osnet_reports_graph_execution_failure_without_fallback(
    tmp_path, monkeypatch, during_startup
):
    model, root, _, network = _model(tmp_path, monkeypatch)
    if during_startup:
        network.fail = True
        with pytest.raises(ValueError, match="1x3x256x128"):
            OsNetIdentity(model, models_root=root)
    else:
        plugin = OsNetIdentity(model, models_root=root)
        network.fail = True
        job, crop = _crop(tmp_path, _person())
        with pytest.raises(ValueError, match="1x3x256x128"):
            plugin.process(job, crop)


@pytest.mark.parametrize(
    "color", [(0, 0, 0), (255, 255, 255), (0, 0, 255), (80, 80, 80)]
)
def test_constant_jpeg_never_emits_descriptor_even_when_network_would_return_features(
    tmp_path, monkeypatch, color
):
    model, root, _, network = _model(tmp_path, monkeypatch)
    plugin = OsNetIdentity(model, models_root=root)
    job, crop = _crop(tmp_path, np.full((80, 40, 3), color, dtype=np.uint8))
    with pytest.raises(ValueError, match="spatial image information"):
        plugin.process(job, crop)
    assert (
        network.calls == 1
    )  # 초기 시험 추론 외에 정보 없는 crop은 DNN에 보내지 않는다.


@pytest.mark.parametrize("levels", [(2, 8), (128, 129)])
def test_dark_or_low_contrast_but_nonconstant_crop_is_not_arbitrarily_rejected(
    tmp_path, monkeypatch, levels
):
    model, root, _, _ = _model(tmp_path, monkeypatch)
    plugin = OsNetIdentity(model, models_root=root)
    image = np.full((80, 40, 3), levels[0], dtype=np.uint8)
    image[40:] = levels[1]
    job, crop = _crop(tmp_path, image)
    assert plugin.process(job, crop)["outcome"] == "complete"


@pytest.mark.parametrize(
    "invalid",
    ["outside", "extension", "empty_file", "too_large", "broken", "multiple_outputs"],
)
def test_invalid_model_fails_before_processing(tmp_path, monkeypatch, invalid):
    model, root, _, network = _model(tmp_path, monkeypatch)
    if invalid == "outside":
        model = tmp_path / "outside.onnx"
        model.write_bytes(b"outside")
    elif invalid == "extension":
        model = root / "model.pt"
        model.write_bytes(b"wrong format")
    elif invalid == "empty_file":
        model.write_bytes(b"")
    elif invalid == "too_large":
        monkeypatch.setattr(appearance, "_MODEL_MAX_BYTES", 8)
    elif invalid == "broken":

        def broken(_):
            raise cv2.error("invalid ONNX")

        monkeypatch.setattr(cv2.dnn, "readNetFromONNX", broken)
    elif invalid == "multiple_outputs":
        network.outputs = ("embeddings", "logits")
    with pytest.raises(ValueError):
        OsNetIdentity(model, models_root=root)


@pytest.mark.parametrize("invalid", ["tiny", "corrupt", "bbox", "missing"])
def test_osnet_shared_crop_validation_precedes_inference(
    tmp_path, monkeypatch, invalid
):
    model, root, _, network = _model(tmp_path, monkeypatch)
    plugin = OsNetIdentity(model, models_root=root)
    image = np.zeros((8, 8, 3), dtype=np.uint8) if invalid == "tiny" else _person()
    job, crop = _crop(tmp_path, image)
    if invalid == "corrupt":
        crop.write_bytes(b"not JPEG")
    elif invalid == "bbox":
        job["object_observation"]["bbox"][2] -= 1
    elif invalid == "missing":
        crop.unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        plugin.process(job, crop)
    assert network.calls == 1


@pytest.mark.asyncio
async def test_constant_crop_reports_failure_without_submitting_identity_descriptor(
    tmp_path, monkeypatch
):
    model, root, _, network = _model(tmp_path, monkeypatch)
    plugin = OsNetIdentity(model, models_root=root)
    observation, crop = _crop(tmp_path, np.zeros((80, 40, 3), dtype=np.uint8))
    job = {**observation, "id": 1, "lease_id": "a" * 32}
    reports = []

    def handler(request):
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json={"job": job})
        reports.append(json.loads(request.content))
        return httpx.Response(200, json={"accepted": True})

    async with httpx.AsyncClient(
        base_url="http://data", transport=httpx.MockTransport(handler)
    ) as client:
        worker = ObjectWorker("identity", client, crop.parent, plugin)
        assert await worker.once()
    assert len(reports) == 1
    assert reports[0]["outcome"] == "failed"
    assert "identity_descriptor" not in reports[0]
    assert reports[0]["global_person_id"] is None
    assert reports[0]["metadata"]["error_code"] == "INVALID_OBJECT_RESULT_OR_CROP"
    assert network.calls == 1
