# 사용자가 준비한 모델 파일을 검증하고 운영 models 폴더로 복사한다.
# 모델을 자동 다운로드하거나 추론 정확도를 검사하는 코드는 아니다.

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


MAX_MODEL_BYTES = 2 * 1024**3
MAX_IDENTITY_MODEL_BYTES = 256 * 1024**2
IDENTITY_MODEL_NAME = "osnet_x0_25_msmt17.onnx"
IDENTITY_MODEL_CONTAINER_PATH = f"/models/{IDENTITY_MODEL_NAME}"
IDENTITY_PLUGIN = "server.services.preprocessing.processors.identity:OsNetIdentity"
GENERIC_IDENTITY_PLUGIN = (
    "server.services.preprocessing.processors.identity:LocalAppearanceIdentity"
)


def resolve_identity_model(
    source_path: Path | None, data_root: Path | None, server_dir: Path
) -> Path:
    """명시한 ONNX 또는 준비된 기본 OSNet을 찾는다. 설치 중 모델 다운로드는 하지 않는다."""
    if source_path is not None:
        return validate_identity_model(source_path)
    candidates = []
    if data_root is not None:
        candidates.append(data_root.expanduser() / "models" / IDENTITY_MODEL_NAME)
    candidates.extend(
        [
            server_dir / "runtime" / "models" / IDENTITY_MODEL_NAME,
            server_dir / "models" / IDENTITY_MODEL_NAME,
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return resolve_identity_model(candidate, data_root, server_dir)
    destination = candidates[0].resolve()
    raise ValueError(
        "OSNet identity model is missing. Prepare it with "
        f'python "{server_dir.resolve() / "tools" / "prepare_osnet.py"}" '
        f'--output "{destination}", or select --identity-model.'
    )


def validate_identity_model(source_path: Path) -> Path:
    source = _validated_local_model(source_path)
    if source.suffix.lower() != ".onnx":
        raise ValueError("identity model must be an OSNet ONNX file")
    if source.stat().st_size > MAX_IDENTITY_MODEL_BYTES:
        raise ValueError("identity ONNX model exceeds the 256 MiB size limit")
    return source


def deployed_identity_model(
    values: dict[str, str], models_root: Path | None
) -> Path | None:
    """기본 OSNet의 컨테이너 경로를 호스트 models 안으로만 대응시킨다."""
    from pathlib import PurePosixPath

    selected = values.get("IDENTITY_MODEL_PATH") or IDENTITY_MODEL_CONTAINER_PATH
    relative = PurePosixPath(selected)
    if (
        models_root is None
        or not relative.is_absolute()
        or relative.parts[:2] != ("/", "models")
        or any(part in {".", ".."} for part in relative.parts)
        or "\\" in selected
        or len(relative.parts) < 3
    ):
        return None
    target = (models_root / Path(*relative.parts[2:])).resolve()
    return target if target.is_relative_to(models_root.resolve()) else None


# 대용량 모델 전체를 메모리에 올리지 않고 1 MiB씩 읽어 내용의 해시를 계산한다.
def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# 지원 확장자와 2 GiB 제한을 확인하고 절대 경로를 반환한다. 모델 역직렬화는 수행하지 않는다.
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
def install_local_model(
    source_path: Path, models_root: Path, *, max_bytes: int | None = None
) -> Path:
    """복사 전후 해시를 비교해 모델 파일의 손상·변경을 확인한 뒤 교체한다."""

    source = _validated_local_model(source_path)
    limit = (
        min(MAX_MODEL_BYTES, max_bytes) if max_bytes is not None else MAX_MODEL_BYTES
    )
    if source.stat().st_size > limit:
        raise ValueError("model file exceeds the configured copy size limit")
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
                # 최초 stat 이후 원본이 커지는 경우도 제한해야 하므로 복사 중 누적 크기를 다시 확인한다.
                total += len(chunk)
                if total > limit:
                    raise ValueError(
                        "model file exceeds the configured copy size limit"
                    )
                copied_digest.update(chunk)
                output_handle.write(chunk)
            output_handle.flush()
            os.fsync(output_handle.fileno())
        if total == 0:
            raise ValueError("model file is empty")
        if copied_digest.hexdigest() != expected_digest:
            raise ValueError(
                "model file changed while it was being copied; retry setup"
            )
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
        if target.stat().st_size != total or sha256_file(target) != expected_digest:
            raise OSError("installed model verification failed")
    finally:
        temporary.unlink(missing_ok=True)
    return target


# 복사 없이 로컬 모델의 경로·크기·확장자 조건만 검사하는 사전 점검 진입점이다.
def validate_custom_model(path: Path) -> None:
    _validated_local_model(path)
