"""실제 JPEG의 외관 특징과 ONNX 전처리 계약을 검증한다. 실제 Re-ID 정확도 평가는 포함하지 않는다."""

import hashlib
from pathlib import Path

import cv2
import numpy as np
import pytest

from server.services.preprocessing.processors.identity import (
    IdentityBlackBox,
    LocalAppearanceIdentity,
)


@pytest.fixture(autouse=True)
def no_deployment_model(monkeypatch):
    # 개발자의 환경변수가 합성 영상의 기본 특징 테스트에 모델을 끼워 넣지 않게 한다.
    monkeypatch.delenv("IDENTITY_MODEL_PATH", raising=False)


def _crop(tmp_path: Path, image: np.ndarray) -> tuple[dict, Path]:
    """JPEG 손실까지 포함해 실제 전처리 서비스가 저장하는 crop 입력을 만든다."""
    height, width = image.shape[:2]
    path = tmp_path / "crop.jpg"
    success, payload = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 98])
    assert success
    path.write_bytes(payload.tobytes())
    return {
        "object_observation": {
            "tracking_session_id": "a" * 32,
            "frame_width": width,
            "frame_height": height,
            "bbox": [0, 0, width, height],
            "crop_path": path.name,
        },
    }, path


def _person() -> np.ndarray:
    # 같은 전체 색 비율을 갖더라도 상·하체 위치가 바뀌면 다른 특징이 되도록 두 색을 배치한다.
    image = np.full((240, 120, 3), (30, 30, 210), dtype=np.uint8)
    image[130:] = (200, 40, 20)
    return image


def _features(result: dict) -> np.ndarray:
    return np.array(result["identity_descriptor"]["features"])


def test_default_descriptor_is_finite_normalized_repeatable_and_has_no_identity(
    tmp_path,
):
    job, path = _crop(tmp_path, _person())
    plugin = LocalAppearanceIdentity()
    first = plugin.process(job, path)
    second = plugin.process({**job, "person_id": "different-local-id"}, path)
    assert first == second
    assert first["outcome"] == "complete"
    assert "global_person_id" not in first
    assert first["identity_descriptor"]["schema_version"] == 1
    assert first["identity_descriptor"]["space_id"] == "appearance-hsv-v1"
    vector = _features(first)
    assert vector.shape == (392,)
    assert np.isfinite(vector).all()
    assert np.linalg.norm(vector) == pytest.approx(1, abs=1e-12)
    assert set(first["metadata"]) == {"backend", "version", "method", "quality"}
    assert first["metadata"]["backend"] == "appearance-hsv-v1"
    assert "features" not in first["metadata"]
    assert list(tmp_path.iterdir()) == [path]


def test_spatial_colors_distinguish_people_with_the_same_overall_palette(tmp_path):
    first_image = _person()
    job, path = _crop(tmp_path, first_image)
    first = _features(LocalAppearanceIdentity().process(job, path))
    job, path = _crop(tmp_path, first_image[::-1])
    swapped = _features(LocalAppearanceIdentity().process(job, path))
    assert first @ swapped < 0.85


def test_weak_brightness_change_and_image_size_keep_similar_descriptors(tmp_path):
    image = _person()
    plugin = LocalAppearanceIdentity()
    job, path = _crop(tmp_path, image)
    original = _features(plugin.process(job, path))
    darker = np.rint(image.astype(np.float32) * 0.80).astype(np.uint8)
    job, path = _crop(tmp_path, darker)
    assert original @ _features(plugin.process(job, path)) > 0.97
    resized = cv2.resize(image, (180, 360), interpolation=cv2.INTER_NEAREST)
    job, path = _crop(tmp_path, resized)
    assert original @ _features(plugin.process(job, path)) > 0.97


def test_black_and_white_are_not_conflated_by_their_undefined_hue(tmp_path):
    plugin = LocalAppearanceIdentity()
    job, path = _crop(tmp_path, np.full((80, 40, 3), 10, dtype=np.uint8))
    black = _features(plugin.process(job, path))
    job, path = _crop(tmp_path, np.full((80, 40, 3), 240, dtype=np.uint8))
    white = _features(plugin.process(job, path))
    assert black @ white < 0.1


class _Net:
    """DNN 입력·출력 계약만 검사하는 대역이며 학습 모델의 인식 성능을 재현하지 않는다."""

    def __init__(self, output=None, outputs=("embedding",)):
        self.output = (
            output if output is not None else np.arange(1, 33, dtype=np.float32)[None]
        )
        self.outputs = outputs
        self.input = None

    def empty(self):
        return False

    def getUnconnectedOutLayersNames(self):
        return self.outputs

    def setPreferableBackend(self, backend):
        assert backend == cv2.dnn.DNN_BACKEND_OPENCV

    def setPreferableTarget(self, target):
        assert target == cv2.dnn.DNN_TARGET_CPU

    def setInput(self, value):
        self.input = value.copy()

    def forward(self):
        return self.output


def _model(tmp_path, monkeypatch, net):
    root = tmp_path / "models"
    root.mkdir(exist_ok=True)
    path = root / "reid.onnx"
    payload = b"opaque model fixture; the net is replaced in this contract test"
    path.write_bytes(payload)

    def load(buffer):
        assert buffer.tobytes() == payload
        return net

    monkeypatch.setattr(cv2.dnn, "readNetFromONNX", load)
    return LocalAppearanceIdentity(path, models_root=root), path, payload


def test_onnx_uses_exact_rgb_nchw_imagenet_input_and_a_model_specific_space(
    tmp_path, monkeypatch
):
    net = _Net()
    plugin, model, payload = _model(tmp_path, monkeypatch, net)
    job, path = _crop(tmp_path, np.full((80, 40, 3), (10, 80, 210), dtype=np.uint8))
    result = plugin.process(job, path)
    assert net.input.shape == (1, 3, 256, 128)
    assert net.input.dtype == np.float32
    decoded = cv2.imdecode(
        np.frombuffer(path.read_bytes(), dtype=np.uint8), cv2.IMREAD_COLOR
    )
    rgb = decoded[40, 20, ::-1].astype(np.float32) / 255
    expected = (rgb - np.array([0.485, 0.456, 0.406])) / np.array([0.229, 0.224, 0.225])
    np.testing.assert_allclose(net.input[0, :, 100, 60], expected, atol=1e-6)
    digest = hashlib.sha256(payload).hexdigest()
    assert (
        result["identity_descriptor"]["space_id"]
        == f"onnx-reid:{digest}:rgb256x128-imagenet-v1"
    )
    assert len(result["identity_descriptor"]["space_id"]) <= 128
    assert np.linalg.norm(_features(result)) == pytest.approx(1)
    assert result["metadata"]["backend"] == "onnx-reid"
    assert model.read_bytes() == payload


@pytest.mark.parametrize(
    "output",
    [
        np.zeros((1, 32), dtype=np.float32),
        np.full((1, 32), np.nan, dtype=np.float32),
        np.full((1, 32), np.inf, dtype=np.float32),
        np.ones((1, 15), dtype=np.float32),
        np.ones((1, 2049), dtype=np.float32),
        np.ones(32, dtype=np.float32),
        np.ones((2, 32), dtype=np.float32),
        np.ones((1, 32), dtype=np.int32),
    ],
)
def test_malformed_onnx_embeddings_fail_without_handcrafted_fallback(
    tmp_path, monkeypatch, output
):
    plugin, _, _ = _model(tmp_path, monkeypatch, _Net(output))
    job, path = _crop(tmp_path, _person())
    with pytest.raises(ValueError):
        plugin.process(job, path)


def test_onnx_with_multiple_outputs_is_rejected_at_startup(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="exactly one"):
        _model(tmp_path, monkeypatch, _Net(outputs=("embedding", "logits")))


@pytest.mark.parametrize(
    "invalid", ["missing", "outside", "extension", "empty", "broken"]
)
def test_invalid_selected_model_never_silently_uses_the_default(
    tmp_path, monkeypatch, invalid
):
    root = tmp_path / "models"
    root.mkdir()
    path = root / "reid.onnx"
    if invalid == "outside":
        path = tmp_path / "outside.onnx"
    elif invalid == "extension":
        path = root / "model.pt"
    if invalid != "missing":
        path.write_bytes(b"not a valid model")
    if invalid == "empty":
        with pytest.raises(ValueError):
            LocalAppearanceIdentity("", models_root=root)
    else:
        with pytest.raises((ValueError, FileNotFoundError)):
            LocalAppearanceIdentity(path, models_root=root)


@pytest.mark.parametrize("invalid", ["tiny", "corrupt", "bbox", "missing"])
def test_invalid_crop_does_not_create_an_identity_descriptor(tmp_path, invalid):
    image = np.zeros((8, 8, 3), dtype=np.uint8) if invalid == "tiny" else _person()
    job, path = _crop(tmp_path, image)
    if invalid == "corrupt":
        path.write_bytes(b"not a JPEG")
    elif invalid == "bbox":
        job["object_observation"]["bbox"][2] -= 1
    elif invalid == "missing":
        path.unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        LocalAppearanceIdentity().process(job, path)


def test_legacy_identity_blackbox_does_not_claim_to_extract_features(tmp_path):
    assert IdentityBlackBox().process({}, tmp_path / "unused.jpg") == {
        "outcome": "unconfigured",
        "metadata": {"reason": "identity_backend_not_implemented"},
    }
