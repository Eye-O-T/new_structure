"""실제 JPEG를 분석해 영역별 색·배경 억제·측정 한계와 잘못된 crop 거부를 검증한다."""

import json
from pathlib import Path

import cv2
import numpy as np
import pytest

from ai_cctv_core.contracts.objects import ObjectJobCompletion
from server.services.analysis.processors import (
    LocalAppearanceAnalyzer,
    MetadataBlackBox,
)


def _write_crop(tmp_path: Path, image: np.ndarray) -> tuple[dict, Path]:
    """알려진 BGR 픽셀을 JPEG로 저장하고 실제 저장 계약에 맞는 관측 정보를 만든다."""
    height, width = image.shape[:2]
    path = tmp_path / "person.jpg"
    success, encoded = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, 98])
    assert success
    path.write_bytes(encoded.tobytes())
    return {
        "object_observation": {
            "tracking_session_id": "a" * 32,
            "bbox": [10, 20, 10 + width, 20 + height],
            "frame_width": width + 20,
            "frame_height": height + 40,
            "crop_path": path.name,
        },
    }, path


def _person_image(
    *, width: int = 120, height: int = 240, narrow: bool = False
) -> np.ndarray:
    """초록 배경 위에 빨간 상체·파란 하체 영역을 두어 전체 평균색 사용을 구별한다."""
    image = np.full((height, width, 3), (20, 180, 20), dtype=np.uint8)
    left, right = (
        (int(width * 0.4), int(width * 0.6)) if narrow else (width // 5, width * 4 // 5)
    )
    image[int(height * 0.15) : int(height * 0.53), left:right] = (25, 25, 220)
    image[int(height * 0.55) : int(height * 0.94), left:right] = (210, 35, 20)
    return image


def test_real_jpeg_has_distinct_upper_and_lower_colors_and_bounded_metadata(tmp_path):
    job, path = _write_crop(tmp_path, _person_image())
    result = LocalAppearanceAnalyzer().process(job, path)
    completion = ObjectJobCompletion.model_validate({**result, "lease_id": "b" * 32})
    assert completion.outcome == "complete"
    assert completion.global_person_id is None
    metadata = result["metadata"]
    assert (
        metadata["backend"],
        metadata["backend_version"],
        metadata["schema_version"],
    ) == (
        "local_appearance",
        "1.0.0",
        1,
    )
    upper = metadata["clothing_colors"]["upper"]["dominant_color"]
    lower = metadata["clothing_colors"]["lower"]["dominant_color"]
    assert upper["name"] == "red"
    assert lower["name"] == "blue"
    assert upper["rgb"][0] > 180 and upper["rgb"][2] < 60
    assert lower["rgb"][2] > 180 and lower["rgb"][0] < 60
    assert metadata["image"]["width"] == 120
    assert metadata["image"]["height"] == 240
    assert metadata["evidence"]["clothing_segmentation"] is False
    assert metadata["evidence"]["confidence_is_probability"] is False
    assert len(json.dumps(metadata, allow_nan=False).encode()) < 65536
    for name in ("upper", "lower"):
        region = metadata["clothing_colors"][name]
        assert 0 < region["dominant_color"]["fraction"] <= 1
        assert 0 <= region["dominant_color"]["confidence"] <= 1
        assert region["sample_pixels"] > 100


def test_edge_background_does_not_dominate_a_narrow_central_person(tmp_path):
    # 초록 픽셀이 영역의 과반이어도 중앙과 양옆의 색 차이를 이용해 사람 후보 영역을 강조한다.
    job, path = _write_crop(tmp_path, _person_image(narrow=True))
    colors = LocalAppearanceAnalyzer().process(job, path)["metadata"]["clothing_colors"]
    for region, expected in ((colors["upper"], "red"), (colors["lower"], "blue")):
        dominant = region["dominant_color"]
        assert dominant["name"] == expected
        assert dominant["fraction"] < 0.5
        assert dominant["weighted_fraction"] > dominant["fraction"]
        assert region["background_discount_applied"]


def test_low_saturation_black_and_white_are_not_assigned_arbitrary_hues(tmp_path):
    image = np.zeros((160, 80, 3), dtype=np.uint8)
    image[80:] = 245
    job, path = _write_crop(tmp_path, image)
    metadata = LocalAppearanceAnalyzer().process(job, path)["metadata"]
    colors = metadata["clothing_colors"]
    assert colors["upper"]["dominant_color"]["name"] == "black"
    assert colors["lower"]["dominant_color"]["name"] == "white"
    assert colors["upper"]["background_discount_applied"] is False
    assert colors["lower"]["background_discount_applied"] is False


def test_multicolor_region_reports_lower_consistency_than_a_solid_color(tmp_path):
    solid = np.full((200, 100, 3), (25, 25, 220), dtype=np.uint8)
    job, path = _write_crop(tmp_path, solid)
    baseline = LocalAppearanceAnalyzer().process(job, path)["metadata"][
        "clothing_colors"
    ]["upper"]
    striped = solid.copy()
    striped[:, 50:] = (210, 35, 20)
    job, path = _write_crop(tmp_path, striped)
    mixed = LocalAppearanceAnalyzer().process(job, path)["metadata"]["clothing_colors"][
        "upper"
    ]
    assert {color["name"] for color in mixed["palette"]} >= {"red", "blue"}
    assert (
        mixed["dominant_color"]["confidence"]
        < baseline["dominant_color"]["confidence"] * 0.6
    )
    assert 0.4 < mixed["dominant_color"]["fraction"] < 0.6


def test_dark_featureless_crop_reports_measurements_without_claiming_blur_or_attributes(
    tmp_path,
):
    job, path = _write_crop(tmp_path, np.zeros((80, 40, 3), dtype=np.uint8))
    metadata = LocalAppearanceAnalyzer().process(job, path)["metadata"]
    quality = metadata["image"]["quality"]
    assert quality["dark_pixel_fraction"] == 1
    assert quality["luminance_mean"] == 0
    assert set(quality["flags"]) == {"mostly_dark", "low_contrast", "low_edge_energy"}
    assert "blur" not in quality["flags"]
    assert set(metadata) == {
        "schema_version",
        "backend",
        "backend_version",
        "image",
        "clothing_colors",
        "evidence",
    }


def test_large_crop_records_original_dimensions_and_limits_sampling_resolution(
    tmp_path,
):
    job, path = _write_crop(tmp_path, _person_image(width=600, height=1200))
    image = LocalAppearanceAnalyzer().process(job, path)["metadata"]["image"]
    assert (image["width"], image["height"]) == (600, 1200)
    assert (image["sample_width"], image["sample_height"]) == (128, 256)


@pytest.mark.parametrize(
    "invalid", ["corrupt", "png", "bbox_mismatch", "tiny", "narrow", "missing"]
)
def test_unsuitable_crop_is_rejected_instead_of_reporting_analysis_success(
    tmp_path, invalid
):
    image = _person_image()
    if invalid == "tiny":
        image = np.zeros((15, 15, 3), dtype=np.uint8)
    elif invalid == "narrow":
        image = np.zeros((1000, 16, 3), dtype=np.uint8)
    job, path = _write_crop(tmp_path, image)
    if invalid == "corrupt":
        path.write_bytes(b"\xff\xd8corrupt-jpeg\xff\xd9")
    elif invalid == "png":
        success, encoded = cv2.imencode(".png", image)
        assert success
        path.write_bytes(encoded.tobytes())
    elif invalid == "bbox_mismatch":
        job["object_observation"]["bbox"][2] -= 1
    elif invalid == "missing":
        path.unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        LocalAppearanceAnalyzer().process(job, path)


def test_legacy_blackbox_remains_explicitly_unconfigured(tmp_path):
    # 이전 팩토리 경로를 쓰는 배포는 새 분석기를 묵시적으로 실행하지 않는다.
    result = MetadataBlackBox().process({}, tmp_path / "unused.jpg")
    assert result == {
        "outcome": "unconfigured",
        "metadata": {"reason": "metadata_backend_not_implemented"},
    }


@pytest.mark.parametrize("limit", ["file_bytes", "pixel_count"])
def test_resource_limits_reject_before_allocating_decoded_pixels(
    tmp_path, monkeypatch, limit
):
    # JPEG의 선언 크기나 파일 크기가 상한을 넘으면 OpenCV 디코더를 호출하기도 전에 실패한다.
    job, path = _write_crop(tmp_path, _person_image())
    if limit == "file_bytes":
        path.write_bytes(b"x" * (8 * 1024 * 1024 + 1))
    else:
        encoded = bytearray(path.read_bytes())
        marker = encoded.index(b"\xff\xc0")
        encoded[marker + 5 : marker + 7] = (6000).to_bytes(2, "big")
        encoded[marker + 7 : marker + 9] = (6000).to_bytes(2, "big")
        path.write_bytes(encoded)
        job["object_observation"].update(
            {
                "bbox": [0, 0, 6000, 6000],
                "frame_width": 6000,
                "frame_height": 6000,
            }
        )

    def unexpected_decode(*_args):
        pytest.fail("Oversized crop reached the image decoder")

    monkeypatch.setattr(cv2, "imdecode", unexpected_decode)
    with pytest.raises(ValueError):
        LocalAppearanceAnalyzer().process(job, path)
