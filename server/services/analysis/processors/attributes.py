"""학습된 ResNet34 다중 헤드 모델로 사람 크롭 한 장의 외관 속성을 예측한다.

모델 파일은 라벨 구성과 표시 규칙을 함께 담은 번들이라, 속성이나 클래스가 바뀐
모델로 교체해도 이 파일을 고칠 필요가 없다. 파일 경로는 PAR_MODEL_PATH로 바꾼다.

옷 색·소지품은 클래스마다 독립 판정이라 여러 개가 함께 나올 수 있고, 성별·연령
같은 속성은 하나만 고른다. 점수는 신뢰도이며 확률이나 정답률이 아니다. 단일
정지 이미지의 추정이므로 가림·조명·해상도에 영향을 받는다.
"""

import math
import os
import re
from collections.abc import Mapping
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
from PIL import Image
from torchvision import transforms
from torchvision.models import resnet34

from ai_cctv_core.processing.images import load_observation_crop

BUNDLE_FORMAT = "par-serving-bundle"
DEFAULT_MODEL_PATH = "/models/par_serving_exp3.pt"
BACKBONE_FEATURE_DIM = 512  # resnet34의 풀링된 특징 차원
MAX_TAGS = 3  # 다중 라벨 속성에서 표시할 최대 개수
SCORE_DIGITS = 4
METADATA_SCHEMA_VERSION = 1
SUPPORTED_FORMAT_VERSION = 1
SUPPORTED_BACKBONE = "resnet34"
ATTRIBUTE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")


class AnalysisModelStartupError(RuntimeError):
    """로그와 서비스 상태에 쓸 수 있는 제한된 모델 준비 오류다."""

    def __init__(self, code: str):
        if code not in {
            "ANALYSIS_MODEL_MISSING", "ANALYSIS_MODEL_INVALID",
            "ANALYSIS_DEVICE_UNAVAILABLE", "ANALYSIS_DEVICE_EXECUTION_FAILED",
        }:
            raise ValueError("Unsupported analysis model startup error")
        self.startup_error_code = code
        super().__init__(code)


class _MultiHeadNetwork(nn.Module):
    """공유 backbone + 속성별 선형 헤드. 번들의 라벨 구성대로 헤드를 만든다."""

    def __init__(self, attributes: dict):
        super().__init__()
        backbone = resnet34(weights=None)
        backbone.fc = nn.Identity()  # backbone.forward가 풀링·평탄화까지 수행한다
        self.backbone = backbone
        self.heads = nn.ModuleDict(
            {name: nn.Linear(BACKBONE_FEATURE_DIM, len(classes)) for name, classes in attributes.items()}
        )

    def forward(self, x):
        features = self.backbone(x)
        return {name: head(features) for name, head in self.heads.items()}


def _load_bundle(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"Model bundle is missing: {path}")
    # 제출 파일은 코드 객체가 아니라 텐서와 기본 자료형만 담는 번들이다. 제한 로더를
    # 사용해 모델 파일이 임의 Python 객체를 역직렬화하지 못하게 한다.
    bundle = torch.load(path, map_location="cpu", weights_only=True)
    if not isinstance(bundle, dict) or bundle.get("format") != BUNDLE_FORMAT:
        raise ValueError(f"Model file is not a {BUNDLE_FORMAT}: {path}")
    for key in (
        "format_version",
        "backbone",
        "attributes",
        "multi_label",
        "display_rules",
        "input_size",
        "normalize",
        "state_dict",
    ):
        if key not in bundle:
            raise ValueError(f"Model bundle is missing '{key}': {path}")
    if (
        isinstance(bundle["format_version"], bool)
        or bundle["format_version"] != SUPPORTED_FORMAT_VERSION
    ):
        raise ValueError("Unsupported model bundle format version")
    if bundle["backbone"] != SUPPORTED_BACKBONE:
        raise ValueError("Unsupported model backbone")

    attributes = bundle["attributes"]
    if not isinstance(attributes, dict) or not attributes:
        raise ValueError("Model attributes must be a nonempty object")
    for name, classes in attributes.items():
        if not isinstance(name, str) or not ATTRIBUTE_NAME.fullmatch(name):
            raise ValueError("Model attribute names must be safe identifiers")
        if (
            not isinstance(classes, list)
            or not classes
            or any(not isinstance(label, str) or not label.strip() for label in classes)
            or len(set(classes)) != len(classes)
        ):
            raise ValueError(f"Model attribute '{name}' has invalid classes")

    multi_label = bundle["multi_label"]
    if (
        not isinstance(multi_label, list)
        or any(not isinstance(name, str) or name not in attributes for name in multi_label)
        or len(set(multi_label)) != len(multi_label)
    ):
        raise ValueError("Model multi-label attributes are invalid")

    display_rules = bundle["display_rules"]
    if not isinstance(display_rules, dict) or any(
        name not in multi_label for name in display_rules
    ):
        raise ValueError("Model display rules reference an unknown attribute")
    for name, rule in display_rules.items():
        if not isinstance(rule, dict) or set(rule) - {"always_top", "threshold"}:
            raise ValueError(f"Model display rule for '{name}' is invalid")
        always_top = rule.get("always_top", False)
        threshold = rule.get("threshold")
        if not isinstance(always_top, bool) or (
            threshold is not None
            and (
                isinstance(threshold, bool)
                or not isinstance(threshold, (int, float))
                or not math.isfinite(threshold)
                or not 0 <= threshold <= 1
            )
        ):
            raise ValueError(f"Model display rule for '{name}' is invalid")

    input_size = bundle["input_size"]
    if (
        not isinstance(input_size, (list, tuple))
        or len(input_size) != 2
        or any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in input_size
        )
    ):
        raise ValueError("Model input size must contain two positive integers")

    normalize = bundle["normalize"]
    if not isinstance(normalize, dict) or set(normalize) != {"mean", "std"}:
        raise ValueError("Model normalization is invalid")
    for key in ("mean", "std"):
        values = normalize[key]
        if (
            not isinstance(values, (list, tuple))
            or len(values) != 3
            or any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or (key == "std" and value <= 0)
                for value in values
            )
        ):
            raise ValueError(f"Model normalization '{key}' is invalid")
    if not isinstance(bundle["state_dict"], Mapping) or not bundle["state_dict"]:
        raise ValueError("Model state_dict must be nonempty")
    version = bundle.get("model_version", "unknown")
    if not isinstance(version, str) or not version.strip():
        raise ValueError("Model version must be a nonempty string")
    return bundle


def _select_multi_label(scores: np.ndarray, rule: dict) -> np.ndarray:
    """번들의 표시 규칙대로 내보낼 클래스를 고른다.

    threshold는 추가 클래스의 하한이고, always_top은 점수가 낮아도 1등 클래스를
    항상 포함한다(옷은 누구나 입고 있으므로 색이 비는 편이 더 틀린 표시가 된다)."""
    threshold = rule.get("threshold")
    picked = scores >= threshold if threshold is not None else np.zeros(scores.shape, dtype=bool)
    if rule.get("always_top"):
        picked[int(scores.argmax())] = True
    return picked


class PersonAttributeAnalyzer:
    # 상태를 갖지 않으므로 시간 초과로 자식이 죽어도 그대로 다시 만들어 쓸 수 있다.
    restartable = True

    def __init__(self):
        gpu_value = os.getenv("PREPROCESSING_GPU", "false").strip().lower()
        if gpu_value not in {"1", "true", "yes", "on", "0", "false", "no", "off"}:
            raise AnalysisModelStartupError("ANALYSIS_DEVICE_UNAVAILABLE")
        gpu_requested = gpu_value in {"1", "true", "yes", "on"}
        requested = os.getenv("ANALYSIS_DEVICE", "auto").strip().lower()
        if re.fullmatch(r"(?:auto|cpu|cuda(?::[0-9]+)?)", requested) is None:
            raise AnalysisModelStartupError("ANALYSIS_DEVICE_UNAVAILABLE")
        if requested.startswith("cuda") and not gpu_requested:
            raise AnalysisModelStartupError("ANALYSIS_DEVICE_UNAVAILABLE")
        self.requested_device = requested
        self._device = torch.device(
            ("cuda:0" if gpu_requested else "cpu") if requested == "auto"
            else ("cuda:0" if requested == "cuda" else requested)
        )
        model_path = Path(os.environ.get("PAR_MODEL_PATH", DEFAULT_MODEL_PATH))
        if not model_path.is_file():
            raise AnalysisModelStartupError("ANALYSIS_MODEL_MISSING")
        try:
            bundle = _load_bundle(model_path)
            self._attributes = bundle["attributes"]
            self._multi_label = set(bundle["multi_label"])
            self._display_rules = bundle["display_rules"]
            self._version = bundle.get("model_version", "unknown")
            self._network = _MultiHeadNetwork(self._attributes)
            self._network.load_state_dict(bundle["state_dict"])
            if self._device.type == "cuda" and (
                not torch.cuda.is_available()
                or self._device.index >= torch.cuda.device_count()
            ):
                raise AnalysisModelStartupError("ANALYSIS_DEVICE_UNAVAILABLE")
            try:
                self._network.to(self._device)
            except Exception as exc:
                raise AnalysisModelStartupError("ANALYSIS_DEVICE_EXECUTION_FAILED") from exc
            self._network.eval()
            height, width = bundle["input_size"]
            self._transform = transforms.Compose([
                transforms.Resize((height, width)),
                transforms.ToTensor(),
                transforms.Normalize(bundle["normalize"]["mean"], bundle["normalize"]["std"]),
            ])
        except FileNotFoundError as exc:
            raise AnalysisModelStartupError("ANALYSIS_MODEL_MISSING") from exc
        except AnalysisModelStartupError:
            raise
        except Exception as exc:
            raise AnalysisModelStartupError("ANALYSIS_MODEL_INVALID") from exc
        try:
            self._network(torch.zeros((1, 3, height, width), device=self._device))
        except Exception as exc:
            raise AnalysisModelStartupError("ANALYSIS_DEVICE_EXECUTION_FAILED") from exc

    def runtime_metadata(self):
        return {
            "requested_device": self.requested_device,
            "execution_device": str(self._device),
            "execution_provider": (
                "PyTorch CUDA" if self._device.type == "cuda" else "PyTorch CPU"
            ),
        }

    @torch.inference_mode()
    def process(self, job: dict, crop_path: Path) -> dict:
        # 공용 로더가 파일 크기·픽셀 수와 관측 bbox 일치를 디코딩 전후로 검사한다.
        # 여기서 나는 ValueError는 공통 실행기가 failed로 보고한다.
        image = load_observation_crop(job, crop_path)
        height, width = image.shape[:2]
        if min(height, width) < 16:
            raise ValueError("Attribute crop must be at least 16 pixels in each dimension")

        rgb = np.ascontiguousarray(image[:, :, ::-1])
        batch = self._transform(Image.fromarray(rgb)).unsqueeze(0).to(self._device)
        outputs = self._network(batch)

        attributes = {}
        for name, classes in self._attributes.items():
            logits = outputs[name][0].float()
            if name in self._multi_label:
                scores = torch.sigmoid(logits).cpu().numpy()
                picked = _select_multi_label(scores, self._display_rules.get(name, {}))
                order = np.argsort(-scores)
                attributes[name] = [
                    {"value": classes[i], "score": round(float(scores[i]), SCORE_DIGITS)}
                    for i in order if picked[i]
                ][:MAX_TAGS]
            else:
                scores = torch.softmax(logits, dim=0).cpu().numpy()
                best = int(scores.argmax())
                attributes[name] = {
                    "value": classes[best],
                    "score": round(float(scores[best]), SCORE_DIGITS),
                }

        return {
            "outcome": "complete",
            "metadata": {
                "schema_version": METADATA_SCHEMA_VERSION,
                "backend": "par_resnet34",
                "backend_version": self._version,
                "image": {"width": width, "height": height},
                "attributes": attributes,
            },
        }
