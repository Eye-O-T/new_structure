"""단일 사람 crop에서 측정한 영역별 색과 영상 품질을 반환하는 CPU 분석기다."""

from pathlib import Path

import cv2
import numpy as np

from ai_cctv_core.processing.images import load_observation_crop


# 색 이름은 사람이 이해하기 위한 거친 HSV 구간이다. 실제 측정 RGB도 함께 반환한다.
_COLOR_NAMES = (
    "black",
    "white",
    "gray",
    "red",
    "orange",
    "yellow",
    "green",
    "cyan",
    "blue",
    "purple",
    "pink",
    "brown",
    "beige",
)
_COLOR_INDEX = {name: index for index, name in enumerate(_COLOR_NAMES)}


def _color_labels(bgr: np.ndarray) -> np.ndarray:
    """밝기·채도로 무채색을 먼저 구분하고 유채색은 색상각으로 분류한다."""
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    hue, saturation, value = (hsv[:, :, index] for index in range(3))
    labels = np.full(hue.shape, _COLOR_INDEX["red"], dtype=np.int32)
    for name, start, end in (
        ("orange", 10, 24),
        ("yellow", 24, 36),
        ("green", 36, 86),
        ("cyan", 86, 101),
        ("blue", 101, 131),
        ("purple", 131, 151),
        ("pink", 151, 170),
    ):
        labels[(hue >= start) & (hue < end)] = _COLOR_INDEX[name]
    labels[(hue >= 5) & (hue < 24) & (value < 170)] = _COLOR_INDEX["brown"]
    labels[(hue >= 10) & (hue < 36) & (saturation <= 110) & (value >= 170)] = (
        _COLOR_INDEX["beige"]
    )
    labels[saturation <= 45] = _COLOR_INDEX["gray"]
    labels[(saturation <= 35) & (value >= 205)] = _COLOR_INDEX["white"]
    labels[value <= 50] = _COLOR_INDEX["black"]
    return labels


def _distribution(labels: np.ndarray) -> np.ndarray:
    """각 색상 구간의 픽셀 비율을 계산하며 빈 표본은 호출자 오류로 거부한다."""
    if labels.size == 0:
        raise ValueError("Color region has no pixels")
    return np.bincount(labels.ravel(), minlength=len(_COLOR_NAMES)) / labels.size


def _region_colors(image: np.ndarray, y_start: float, y_end: float) -> dict:
    """중앙 가중치와 양옆 배경 표본으로 영역의 대표색을 추정한다.

    의복 분할 모델이 없으므로 상·하체의 기하학적 영역을 사용한다. 양옆과 중앙이
    서로 다른 색일 때만 양옆 색의 가중치를 낮춰 단색 의복을 배경으로 지우지 않는다.
    """
    height, width = image.shape[:2]
    y0, y1 = int(height * y_start), max(int(height * y_end), int(height * y_start) + 1)
    x0, x1 = int(width * 0.16), max(int(width * 0.84), int(width * 0.16) + 1)
    band = image[y0:y1]
    all_labels = _color_labels(band)
    labels = all_labels[:, x0:x1]
    region = band[:, x0:x1]
    edge_width = max(1, int(width * 0.12))
    edge_labels = np.concatenate(
        (all_labels[:, :edge_width], all_labels[:, -edge_width:]), axis=1
    )
    middle = all_labels[
        :, int(width * 0.4) : max(int(width * 0.6), int(width * 0.4) + 1)
    ]
    background = _distribution(edge_labels)
    central = _distribution(middle)
    separation = float(np.abs(central - background).sum() * 0.5)
    background_strength = min(1.0, separation / 0.35)

    # 중앙을 강조하되 완전히 버리는 픽셀은 없다. 배경 억제 전 비율도 따로 보존한다.
    horizontal = (np.arange(x0, x1) + 0.5) / width
    spatial = np.exp(-0.5 * ((horizontal - 0.5) / 0.22) ** 2)
    weights = np.broadcast_to(spatial, labels.shape).copy()
    weights *= 1.0 - 0.85 * background[labels] * background_strength
    weighted = np.bincount(
        labels.ravel(), weights=weights.ravel(), minlength=len(_COLOR_NAMES)
    )
    weighted /= weighted.sum()
    raw = _distribution(labels)
    ranking = np.argsort(-weighted, kind="stable")
    first, second = float(weighted[ranking[0]]), float(weighted[ranking[1]])
    margin = max(0.0, (first - second) / max(first, 1e-12))
    confidence = first * (0.5 + 0.5 * margin)
    confidence *= 1.0 - 0.5 * background[ranking[0]] * background_strength

    palette = []
    for index in ranking[:3]:
        if raw[index] == 0:
            continue
        # 구간 내 중앙값은 소수의 JPEG 경계 잡음이나 반사광이 RGB를 바꾸는 영향을 줄인다.
        rgb = np.median(region[labels == index, ::-1], axis=0)
        palette.append(
            {
                "name": _COLOR_NAMES[index],
                "rgb": [int(round(channel)) for channel in rgb],
                "fraction": round(float(raw[index]), 4),
                "weighted_fraction": round(float(weighted[index]), 4),
            }
        )
    return {
        "region_normalized_xyxy": [0.16, y_start, 0.84, y_end],
        "sample_region_xyxy": [x0, y0, x1, y1],
        "sample_pixels": int(labels.size),
        "dominant_color": {**palette[0], "confidence": round(float(confidence), 4)},
        "palette": palette,
        "background_color_separation": round(separation, 4),
        "background_discount_applied": background_strength > 0,
    }


def _quality(image: np.ndarray) -> dict:
    """촬영 조건을 추측하지 않고 표본 픽셀의 밝기·대비·고주파 에너지를 측정한다."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    luminance = float(gray.mean())
    contrast = float(gray.std())
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    flags = []
    if luminance < 25:
        flags.append("mostly_dark")
    if luminance > 235:
        flags.append("mostly_bright")
    if contrast < 8:
        flags.append("low_contrast")
    if sharpness < 25:
        # 단색 영역도 값이 낮을 수 있으므로 저초점·흐림으로 단정하지 않는다.
        flags.append("low_edge_energy")
    return {
        "luminance_mean": round(luminance, 4),
        "luminance_stddev": round(contrast, 4),
        "laplacian_variance": round(sharpness, 4),
        "dark_pixel_fraction": round(float(np.mean(gray <= 5)), 4),
        "bright_pixel_fraction": round(float(np.mean(gray >= 250)), 4),
        "flags": flags,
    }


class LocalAppearanceAnalyzer:
    """외부 모델 없이 crop의 상·하의 후보 영역 색과 이미지 품질을 분석한다.

    사람 자세나 의복 경계를 알아내는 모델이 아니다. 단일 프레임의 관측값만
    반환하며 인물 ID·인구통계적 속성·행동·가려진 의복은 판정하지 않는다.
    """

    def process(self, job: dict, crop_path: Path) -> dict:
        # 공용 로더가 JPEG 크기 제한과 관측 bbox 일치를 디코딩 전후로 검사한다.
        image = load_observation_crop(job, crop_path)
        height, width = image.shape[:2]
        if min(height, width) < 16:
            raise ValueError(
                "Appearance crop must be at least 16 pixels in each dimension"
            )

        # 연산량과 품질 지표의 측정 해상도를 제한한다. 원본 크기는 별도 필드로 유지한다.
        scale = min(1.0, 256 / max(height, width))
        sample = image
        if scale < 1:
            sample = cv2.resize(
                image,
                (max(1, round(width * scale)), max(1, round(height * scale))),
                interpolation=cv2.INTER_AREA,
            )
        if min(sample.shape[:2]) < 8:
            raise ValueError(
                "Appearance crop is too narrow for regional color sampling"
            )
        return {
            "outcome": "complete",
            "metadata": {
                "schema_version": 1,
                "backend": "local_appearance",
                "backend_version": "1.0.0",
                "image": {
                    "width": width,
                    "height": height,
                    "sample_width": int(sample.shape[1]),
                    "sample_height": int(sample.shape[0]),
                    "quality": _quality(sample),
                },
                "clothing_colors": {
                    "upper": _region_colors(sample, 0.23, 0.50),
                    "lower": _region_colors(sample, 0.58, 0.88),
                },
                "evidence": {
                    "source": "single_person_crop",
                    "method": "central_region_hsv_palette_v1",
                    "clothing_segmentation": False,
                    "fraction_basis": "unweighted_pixels_in_sample_region",
                    "confidence_basis": "heuristic_weighted_support_and_color_margin",
                    "confidence_is_probability": False,
                    "limitations": [
                        "Regions estimate clothing location geometrically; pose and occlusion can mix other pixels.",
                        "Lighting and JPEG compression change observed colors.",
                        "Side colors only approximate background; matching foreground and background cannot be separated.",
                        "Quality values describe the resized sample; low edge energy alone does not prove blur.",
                    ],
                },
            },
        }
