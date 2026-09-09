"""사람 crop의 외관 특징만 추출하며 인물 연결 상태는 Data 서비스에 맡긴다."""

import hashlib
import os
from pathlib import Path
import stat

import cv2
import numpy as np

from ai_cctv_core.processing.images import load_observation_crop


_MODEL_MAX_BYTES = 256 * 1024 * 1024
_ONNX_PREPROCESSING = "rgb256x128-imagenet-v1"


def _read_onnx_model(model_path: str | Path, models_root: Path) -> tuple[bytes, str]:
    """허용된 모델 폴더의 단일 ONNX를 제한 크기로 읽고 동일 바이트의 지문을 반환한다."""
    if not str(model_path).strip():
        raise ValueError("IDENTITY_MODEL_PATH must name an ONNX model")
    path = Path(model_path).resolve(strict=True)
    if not path.is_relative_to(models_root.resolve()) or path.suffix.lower() != ".onnx":
        raise ValueError(
            "Identity model must be an ONNX file inside the models directory"
        )
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= _MODEL_MAX_BYTES:
        raise ValueError("Identity model file size or type is invalid")
    with path.open("rb") as handle:
        payload = handle.read(_MODEL_MAX_BYTES + 1)
    if len(payload) > _MODEL_MAX_BYTES:
        raise ValueError("Identity model exceeds the size limit")
    return payload, hashlib.sha256(payload).hexdigest()


def _load_onnx_net(payload: bytes):
    """검사·해시 계산을 마친 동일 모델 바이트를 OpenCV CPU 실행기에 전달한다."""
    try:
        network = cv2.dnn.readNetFromONNX(np.frombuffer(payload, dtype=np.uint8))
        network.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
        network.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        if network.empty() or len(network.getUnconnectedOutLayersNames()) != 1:
            raise ValueError(
                "Identity ONNX model must have exactly one embedding output"
            )
    except cv2.error as error:
        raise ValueError("Identity ONNX model cannot be loaded") from error
    return network


def _onnx_input(image: np.ndarray) -> np.ndarray:
    """RGB 256×128에 ImageNet 평균·표준편차를 적용한 연속 float32 NCHW 입력이다."""
    resized = cv2.resize(image, (128, 256), interpolation=cv2.INTER_LINEAR)
    rgb = cv2.cvtColor(resized, cv2.COLOR_BGR2RGB).astype(np.float32) / 255
    mean = np.array([0.485, 0.456, 0.406], dtype=np.float32)
    std = np.array([0.229, 0.224, 0.225], dtype=np.float32)
    return np.ascontiguousarray(((rgb - mean) / std).transpose(2, 0, 1)[None])


def _normalized(values: np.ndarray) -> list[float]:
    """벡터 길이·유한값·영벡터를 검사한 뒤 비교용 단위 길이로 정규화한다."""
    vector = np.asarray(values)
    if vector.ndim != 1 or not 16 <= vector.size <= 2048 or vector.dtype.kind != "f":
        raise ValueError(
            "Identity embedding must contain 16..2048 floating point features"
        )
    vector = vector.astype(np.float64)
    if not np.isfinite(vector).all():
        raise ValueError("Identity embedding contains non-finite values")
    norm = float(np.linalg.norm(vector))
    if not np.isfinite(norm) or norm <= 1e-12:
        raise ValueError("Identity embedding must have a finite nonzero norm")
    return (vector / norm).tolist()


def _root_histogram(counts: np.ndarray) -> np.ndarray:
    """히스토그램을 확률의 제곱근으로 바꾸어 일부 많은 픽셀의 영향만 커지지 않게 한다."""
    total = float(counts.sum())
    return np.sqrt(counts / total) if total > 0 else np.zeros(counts.shape)


def _color_histogram(hsv: np.ndarray) -> np.ndarray:
    """유채색의 원형 hue·채도와 무채색의 밝기를 별도 구간에 기록한다."""
    hue, saturation, value = (hsv[:, :, index].astype(np.float64) for index in range(3))
    chromatic = (saturation >= 32) & (value >= 50)
    h = hue[chromatic] / 180 * 18
    s = saturation[chromatic] / 255 * 3
    h0, s0 = np.floor(h).astype(int), np.floor(s).astype(int)
    dh, ds = h - h0, s - s0
    histogram = np.zeros((18, 4), dtype=np.float64)
    # 경계에 걸친 색은 이웃 구간에도 배분하며 red의 0/180도 경계는 원형으로 잇는다.
    for h_index, h_weight in ((h0, 1 - dh), ((h0 + 1) % 18, dh)):
        for s_index, s_weight in ((s0, 1 - ds), (np.minimum(s0 + 1, 3), ds)):
            np.add.at(histogram, (h_index, s_index), h_weight * s_weight)
    histogram = (
        0.6 * histogram
        + 0.2 * np.roll(histogram, 1, axis=0)
        + 0.2 * np.roll(histogram, -1, axis=0)
    )
    # 무채색에서 의미 없는 hue를 사용하지 않아 흰색·회색·검정색 의복을 구분한다.
    neutral = np.bincount((value[~chromatic] // 32).astype(int), minlength=8).astype(
        np.float64
    )
    return _root_histogram(np.concatenate((histogram.ravel(), neutral)))


def _handcrafted_features(image: np.ndarray) -> np.ndarray:
    """상·하체의 네 수평 띠마다 색·밝기·경사 특징을 쌓아 위치 정보를 보존한다."""
    resized = cv2.resize(image, (128, 256), interpolation=cv2.INTER_AREA)
    hsv = cv2.cvtColor(resized, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(resized, cv2.COLOR_BGR2GRAY).astype(np.float64)
    features = []
    for top, bottom in ((0.20, 0.375), (0.375, 0.55), (0.55, 0.725), (0.725, 0.90)):
        rows = slice(int(256 * top), int(256 * bottom))
        columns = slice(20, 108)
        region_hsv, region_gray = hsv[rows, columns], gray[rows, columns]
        colors = _color_histogram(region_hsv)
        luminance = np.bincount(
            (region_gray.ravel() // 32).astype(int), minlength=8
        ).astype(np.float64)
        gx = cv2.Sobel(region_gray, cv2.CV_64F, 1, 0, ksize=3)
        gy = cv2.Sobel(region_gray, cv2.CV_64F, 0, 1, ksize=3)
        magnitude = np.hypot(gx, gy)
        orientation = np.mod(np.arctan2(gy, gx), np.pi)
        directions = np.minimum((orientation / np.pi * 8).astype(int), 7)
        gradients = np.bincount(
            directions.ravel(), weights=magnitude.ravel(), minlength=8
        )
        texture = np.array(
            [
                min(1.0, float(region_gray.std()) / 128),
                min(1.0, float(magnitude.mean()) / 255),
            ]
        )
        # 색을 주 신호로 삼고 밝기·질감은 보조 신호로 제한해 약한 노출 변화에 덜 민감하게 한다.
        features.append(
            np.concatenate(
                (
                    colors,
                    0.12 * _root_histogram(luminance),
                    0.20 * _root_histogram(gradients),
                    0.05 * texture,
                )
            )
        )
    return np.concatenate(features)


def _quality(image: np.ndarray) -> dict:
    """비교에 사용한 영상의 크기와 밝기·대비를 측정하며 신원 확률은 산출하지 않는다."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return {
        "crop_width": int(image.shape[1]),
        "crop_height": int(image.shape[0]),
        "mean_luminance": round(float(gray.mean()), 4),
        "luminance_stddev": round(float(gray.std()), 4),
        "dark_pixel_fraction": round(float(np.mean(gray <= 5)), 4),
        "bright_pixel_fraction": round(float(np.mean(gray >= 250)), 4),
    }


class LocalAppearanceIdentity:
    """기본 외관 특징 또는 명시적으로 지정한 로컬 ONNX의 특징을 읽기 전용으로 추출한다.

    외관이 비슷한 다른 사람을 구분한다고 보장하지 않는다. 이 플러그인은 gallery를
    저장하거나 global_person_id를 발급하지 않으며 Data의 보수적 비교 정책을 사용한다.
    """

    def __init__(
        self,
        model_path: str | Path | None = None,
        *,
        models_root: Path = Path("/models"),
    ):
        self._net = None
        self._space_id = "appearance-hsv-v1"
        self._backend = "appearance-hsv-v1"
        self._method = "four_body_bands_hsv_luminance_gradient"
        # Compose는 선택 옵션을 빈 환경 변수로 전달한다. 명시적인 생성자 빈 경로는 오류다.
        configured = (
            model_path
            if model_path is not None
            else (os.getenv("IDENTITY_MODEL_PATH") or None)
        )
        if configured is not None:
            # 잘못 지정한 모델을 기본 특징으로 대체하면 서로 다른 특징 공간이 섞이므로 실패시킨다.
            payload, digest = _read_onnx_model(configured, models_root)
            self._net = _load_onnx_net(payload)
            self._space_id = f"onnx-reid:{digest}:{_ONNX_PREPROCESSING}"
            self._backend = "onnx-reid"
            self._method = _ONNX_PREPROCESSING

    def _onnx_features(self, image: np.ndarray) -> np.ndarray:
        """고정 RGB/ImageNet 전처리의 1×3×256×128 입력과 단일 1×D 출력을 강제한다."""
        blob = _onnx_input(image)
        try:
            self._net.setInput(blob)
            output = self._net.forward()
        except cv2.error as error:
            raise ValueError(
                "Identity ONNX model cannot produce its embedding"
            ) from error
        if (
            not isinstance(output, np.ndarray)
            or output.ndim != 2
            or output.shape[0] != 1
        ):
            raise ValueError("Identity ONNX output must be a single 1xD embedding")
        return output[0]

    def process(self, job: dict, crop_path: Path) -> dict:
        image = load_observation_crop(job, crop_path)
        if min(image.shape[:2]) < 16:
            raise ValueError(
                "Identity crop must be at least 16 pixels in each dimension"
            )
        vector = (
            _handcrafted_features(image)
            if self._net is None
            else self._onnx_features(image)
        )
        return {
            "outcome": "complete",
            "identity_descriptor": {
                "schema_version": 1,
                "space_id": self._space_id,
                "features": _normalized(vector),
            },
            # 특징 벡터는 내부 비교 계약에만 담으며 일반 이벤트 metadata에는 복제하지 않는다.
            "metadata": {
                "backend": self._backend,
                "version": "1.0.0",
                "method": self._method,
                "quality": _quality(image),
            },
        }
