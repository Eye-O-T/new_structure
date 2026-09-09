"""학습된 OSNet x0.25 ONNX로 사람 재식별 특징을 추출한다. 모델 미준비 시 대체하지 않는다."""

import os
from pathlib import Path

import cv2
import numpy as np

from ai_cctv_core.processing.images import load_observation_crop

from .appearance import (
    _ONNX_PREPROCESSING,
    _load_onnx_net,
    _normalized,
    _onnx_input,
    _quality,
    _read_onnx_model,
)


DEFAULT_MODEL_FILENAME = "osnet_x0_25_msmt17.onnx"


class OsNetIdentity:
    """고정 입력과 512차원 출력을 검사하며 ID 부여·gallery 저장은 Data에 위임한다.

    모델의 학습 출처·ONNX 그래프 선언은 배포용 내보내기 도구에서 검증한다. 여기서는
    실제 DNN 실행과 출력 형식을 확인하며 앞뒤 모습의 동일인 판정을 보장하지 않는다.
    """

    def __init__(
        self,
        model_path: str | Path | None = None,
        *,
        models_root: Path = Path("/models"),
    ):
        # 빈 Compose 옵션도 기본 OSNet 파일을 요구한다. HSV로 전환되는 경로는 없다.
        configured = (
            model_path
            if model_path is not None
            else (
                os.getenv("IDENTITY_MODEL_PATH") or models_root / DEFAULT_MODEL_FILENAME
            )
        )
        payload, self._model_sha256 = _read_onnx_model(configured, models_root)
        self._space_id = f"osnet:{self._model_sha256}:{_ONNX_PREPROCESSING}"
        self._net = _load_onnx_net(payload)
        # 준비 상태를 알리기 전에 실제 추론해 OpenCV 연산 호환성과 512차원 계약을 검사한다.
        # 이 시험 표본의 결과는 버리고 완료 요청이나 gallery에 보내지 않는다.
        self._features(np.zeros((256, 128, 3), dtype=np.uint8))

    def _features(self, image: np.ndarray) -> list[float]:
        try:
            self._net.setInput(_onnx_input(image))
            output = self._net.forward()
        except cv2.error as error:
            raise ValueError(
                "OSNet cannot run its 1x3x256x128 float32 input"
            ) from error
        if (
            not isinstance(output, np.ndarray)
            or output.shape != (1, 512)
            or output.dtype != np.float32
        ):
            raise ValueError("OSNet output must be one float32 1x512 embedding")
        return _normalized(output[0])

    def process(self, job: dict, crop_path: Path) -> dict:
        image = load_observation_crop(job, crop_path)
        if min(image.shape[:2]) < 16:
            raise ValueError("OSNet crop must be at least 16 pixels in each dimension")
        # 완전히 단색인 JPEG도 DNN은 비영 특징을 낼 수 있다. 정보 없는 표본만 차단하며
        # 어두움·흐림·낮은 대비 자체를 임의 임계값으로 제외하지 않는다.
        if np.all(image.max(axis=(0, 1)) == image.min(axis=(0, 1))):
            raise ValueError("OSNet crop must contain spatial image information")
        return {
            "outcome": "complete",
            "identity_descriptor": {
                "schema_version": 1,
                "space_id": self._space_id,
                "features": self._features(image),
            },
            "metadata": {
                "backend": "osnet",
                "version": "1.0.0",
                "architecture": "osnet_x0_25",
                "model_sha256": self._model_sha256,
                "method": _ONNX_PREPROCESSING,
                "quality": _quality(image),
            },
        }
