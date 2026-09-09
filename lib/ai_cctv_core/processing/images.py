"""공유 크롭을 제한된 크기로 읽고 원본 관측과 일치하는 JPEG만 디코딩한다."""

from pathlib import Path
import stat

from ai_cctv_core.contracts.objects import ObjectObservation


def _jpeg_dimensions(payload: bytes) -> tuple[int, int]:
    # 픽셀 버퍼를 할당하기 전에 SOF의 크기를 확인한다. 실제 디코딩은 OpenCV에 맡긴다.
    if not payload.startswith(b"\xff\xd8") or not payload.endswith(b"\xff\xd9"):
        raise ValueError("Crop must be a complete JPEG")
    offset = 2
    while offset < len(payload) - 2:
        if payload[offset] != 0xFF:
            raise ValueError("Invalid JPEG marker")
        while offset < len(payload) and payload[offset] == 0xFF:
            offset += 1
        if offset >= len(payload):
            break
        marker = payload[offset]
        offset += 1
        if marker in {0x00, 0xD8, 0xD9, 0xDA}:
            break
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            continue
        if offset + 2 > len(payload):
            break
        length = int.from_bytes(payload[offset : offset + 2], "big")
        if length < 2 or offset + length > len(payload):
            break
        # OpenCV가 생성하는 8비트 baseline/progressive JPEG를 허용한다.
        if marker in {0xC0, 0xC1, 0xC2}:
            if length < 8 or payload[offset + 2] != 8:
                break
            height = int.from_bytes(payload[offset + 3 : offset + 5], "big")
            width = int.from_bytes(payload[offset + 5 : offset + 7], "big")
            components = payload[offset + 7]
            if components not in {1, 3} or length != 8 + 3 * components:
                break
            if width > 0 and height > 0:
                return width, height
            break
        offset += length
    raise ValueError("Unsupported or damaged JPEG header")


def load_observation_crop(
    job: dict,
    crop_path: Path,
    *,
    max_file_bytes: int = 8 * 1024 * 1024,
    max_pixels: int = 16_000_000,
):
    """검증된 저장소 경로의 JPEG를 관측 bbox와 같은 크기의 uint8 BGR로 반환한다."""
    import cv2
    import numpy as np

    if not isinstance(job, dict) or "object_observation" not in job:
        raise ValueError("Object observation is required")
    observation = ObjectObservation.model_validate(job["object_observation"])
    path = Path(crop_path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= max_file_bytes:
        raise ValueError("Crop file size or type is invalid")
    # stat 이후 파일이 커져도 제한보다 많이 읽지 않는다.
    with path.open("rb") as handle:
        payload = handle.read(max_file_bytes + 1)
    if len(payload) > max_file_bytes:
        raise ValueError("Crop exceeds the file size limit")
    width, height = _jpeg_dimensions(payload)
    x1, y1, x2, y2 = observation.bbox
    if width * height > max_pixels or (width, height) != (x2 - x1, y2 - y1):
        raise ValueError("Crop dimensions do not match the observation")
    # EXIF 회전으로 bbox와 픽셀 좌표가 어긋나지 않도록 저장된 픽셀 배치를 그대로 읽는다.
    image = cv2.imdecode(
        np.frombuffer(payload, dtype=np.uint8),
        cv2.IMREAD_COLOR | cv2.IMREAD_IGNORE_ORIENTATION,
    )
    if image is None or image.shape != (height, width, 3) or image.dtype != np.uint8:
        raise ValueError("Crop JPEG cannot be decoded")
    return image
