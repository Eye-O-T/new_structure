# 사용자가 준비한 모델 파일을 검증하고 운영 models 폴더로 복사한다.
# 모델을 자동 다운로드하거나 추론 정확도를 검사하는 코드는 아니다.

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


MAX_MODEL_BYTES = 2 * 1024**3


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_local_model(path: Path) -> Path:
    source = path.expanduser()
    if not source.is_file():
        raise ValueError(f"model file does not exist or is not a file: {source}")
    if source.suffix.lower() not in {".pt", ".onnx", ".engine"}:
        raise ValueError("supported model formats are .pt, .onnx and .engine")
    size = source.stat().st_size
    if size == 0:
        raise ValueError("model file is empty")
    if size > MAX_MODEL_BYTES:
        raise ValueError("model file exceeds the 2 GiB size limit")
    return source.resolve()


# SHA-256은 파일 내용의 지문이다. 복사 전후 지문을 비교해 복사 중 변경·손상을 확인한다.
def install_local_model(source_path: Path, models_root: Path) -> Path:
    """복사 전후 해시를 비교해 모델 파일의 손상·변경을 확인한 뒤 교체한다."""

    source = _validated_local_model(source_path)
    target_dir = models_root.expanduser().resolve()
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / source.name
    if source == target.resolve():
        return target

    expected_digest = sha256_file(source)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=target_dir, prefix=f".{target.name}.", suffix=".tmp"
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    copied_digest = hashlib.sha256()
    total = 0
    try:
        with source.open("rb") as input_handle, temporary.open("wb") as output_handle:
            while True:
                chunk = input_handle.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_MODEL_BYTES:
                    raise ValueError("model file exceeds the 2 GiB size limit")
                copied_digest.update(chunk)
                output_handle.write(chunk)
            output_handle.flush()
            os.fsync(output_handle.fileno())
        if total == 0:
            raise ValueError("model file is empty")
        if copied_digest.hexdigest() != expected_digest:
            raise ValueError("model file changed while it was being copied; retry setup")
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
        if target.stat().st_size != total or sha256_file(target) != expected_digest:
            raise OSError("installed model verification failed")
    finally:
        temporary.unlink(missing_ok=True)
    return target


def validate_custom_model(path: Path) -> None:
    _validated_local_model(path)
