"""공식 MSMT17 재식별 가중치를 검증하고 운영용 OSNet ONNX를 준비한다.

이 도구에서만 네트워크와 PyTorch를 사용한다. 서비스는 완성된 ONNX를 읽기만 한다.
소스·가중치의 버전과 해시를 고정하여 ImageNet 가중치나 임의 코드가 섞이지 않게 한다.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import tempfile
import urllib.request


SERVER_ROOT = Path(__file__).resolve().parents[1]
MODEL_NAME = "osnet_x0_25_msmt17.onnx"
SOURCE_REVISION = "f8cd150fdf77e8d9e1ed143b7f308c2c609ded50"
WEIGHTS_REVISION = "a5c5cc037c24235cda3b21085b93ad77c9616224"
WEIGHTS_NAME = (
    "osnet_x0_25_msmt17_combineall_256x128_amsgrad_ep150_stp60_"
    "lr0.0015_b64_fb10_softmax_labelsmooth_flip_jitter.pth"
)
SOURCE_BASE = (
    f"https://raw.githubusercontent.com/KaiyangZhou/deep-person-reid/{SOURCE_REVISION}/"
)
ARTIFACTS = {
    "osnet.py": (
        SOURCE_BASE + "torchreid/models/osnet.py",
        "c7c1c29187d6330f859c91da229271531920464c7011aec13842a086b2263cae",
        128 * 1024,
    ),
    "LICENSE": (
        SOURCE_BASE + "LICENSE",
        "3ac8ce2a83d170cb1c7c84152e0c1faca1f187794303514383960d2441716247",
        16 * 1024,
    ),
    WEIGHTS_NAME: (
        f"https://huggingface.co/kaiyangzhou/osnet/resolve/{WEIGHTS_REVISION}/"
        + WEIGHTS_NAME,
        "cf55163d78fc44c62c82f85ab62d39f10438679b5abe8c698ae08cfa84aa6e18",
        16 * 1024 * 1024,
    ),
}


def _verified_bytes(path: Path, expected: str, limit: int) -> bytes:
    """실행하거나 역직렬화하기 전에 캐시까지 동일한 크기·해시 검사를 적용한다."""
    with path.open("rb") as handle:
        payload = handle.read(limit + 1)
    if not payload or len(payload) > limit:
        raise ValueError(f"Artifact size is invalid: {path.name}")
    if hashlib.sha256(payload).hexdigest() != expected:
        raise ValueError(
            f"SHA-256 mismatch: {path.name}; remove the corrupt cache file"
        )
    return payload


def prepare_artifact(cache: Path, name: str, *, offline: bool) -> Path:
    """중단된 다운로드를 정식 캐시로 취급하지 않으며 기존 정상 파일을 재사용한다."""
    url, expected, limit = ARTIFACTS[name]
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / name
    if target.exists():
        _verified_bytes(target, expected, limit)
        return target
    if offline:
        raise FileNotFoundError(f"Offline cache is missing {name}")
    request = urllib.request.Request(
        url, headers={"User-Agent": "ai-cctv-osnet-setup/1"}
    )
    with tempfile.TemporaryDirectory(prefix=".download-", dir=cache) as temporary:
        partial = Path(temporary) / name
        with urllib.request.urlopen(request, timeout=60) as response:
            if not response.geturl().startswith("https://"):
                raise ValueError("Model downloads must remain on HTTPS")
            with partial.open("wb") as handle:
                total = 0
                while chunk := response.read(1024 * 1024):
                    total += len(chunk)
                    if total > limit:
                        raise ValueError(f"Download exceeds the size limit: {name}")
                    handle.write(chunk)
        _verified_bytes(partial, expected, limit)
        os.replace(partial, target)
    return target


def _load_model(source: Path, weights: Path):
    """전층을 엄격히 로드해 빠진 층을 무작위 초기값으로 남기는 변환을 금지한다."""
    import torch

    # 검증한 바이트를 직접 컴파일하여 검사 뒤 파일 교체로 다른 코드가 실행되지 않게 한다.
    payload = _verified_bytes(
        source, ARTIFACTS["osnet.py"][1], ARTIFACTS["osnet.py"][2]
    )
    specification = importlib.util.spec_from_file_location("_official_osnet", source)
    module = importlib.util.module_from_spec(specification)
    exec(compile(payload, str(source), "exec"), module.__dict__)
    import io

    checkpoint_bytes = _verified_bytes(weights, *ARTIFACTS[WEIGHTS_NAME][1:])
    checkpoint = torch.load(
        io.BytesIO(checkpoint_bytes), map_location="cpu", weights_only=True
    )
    state = checkpoint.get("state_dict", checkpoint)
    state = {key.removeprefix("module."): value for key, value in state.items()}
    classes = state["classifier.weight"].shape[0]
    # pretrained=True는 ImageNet을 별도로 받는 옵션이다. 재식별 체크포인트만 엄격히 적용한다.
    model = module.osnet_x0_25(num_classes=classes, pretrained=False)
    model.load_state_dict(state, strict=True)
    return model.cpu().eval()


def _validate_graph(path: Path) -> None:
    """정적 float32 입력과 분류 점수가 아닌 단일 512차원 임베딩 출력을 확인한다."""
    import onnx

    graph = onnx.load(path, load_external_data=False)
    onnx.checker.check_model(graph)
    if len(graph.graph.input) != 1 or len(graph.graph.output) != 1:
        raise ValueError("OSNet must have exactly one input and one output")
    for value, shape in (
        (graph.graph.input[0], [1, 3, 256, 128]),
        (graph.graph.output[0], [1, 512]),
    ):
        tensor = value.type.tensor_type
        if (
            tensor.elem_type != onnx.TensorProto.FLOAT
            or [d.dim_value for d in tensor.shape.dim] != shape
        ):
            raise ValueError(f"Unexpected OSNet tensor contract: {value.name}")
    if any(
        item.data_location == onnx.TensorProto.EXTERNAL
        for item in graph.graph.initializer
    ):
        raise ValueError("OSNet must contain its weights in one ONNX file")


def _discard_bundle_backup(directory: Path) -> None:
    """이 게시 시도가 만든 백업 파일만 정리하고 실패 시 복구 경로를 드러낸다."""
    try:
        for path in directory.iterdir():
            # copy2가 보존한 읽기 전용 속성도 Windows에서 정리할 수 있게 한다.
            path.chmod(0o600)
            path.unlink()
        directory.rmdir()
    except OSError as error:
        raise OSError(f"Cannot remove OSNet bundle backup: {directory}") from error


def _publish_bundle(files: list[tuple[Path, Path]]) -> None:
    """완성된 모델·부속 파일을 게시하고 일반 I/O 실패 때 바뀐 파일만 복원한다.

    세 파일 전체의 전원 장애·동시 실행 원자성을 보장하지는 않는다. 복원이 실패하면
    원본 백업을 보존하여 운영자가 오류에 표시된 폴더에서 복구할 수 있게 한다.
    """
    backup_directory = Path(
        tempfile.mkdtemp(prefix=".osnet-backup-", dir=files[0][1].parent)
    )
    backups: dict[Path, Path | None] = {}
    changed: list[Path] = []
    try:
        # 원본 복사가 모두 성공하기 전에는 어떤 배포 파일도 교체하지 않는다.
        for _source, target in files:
            backup = backup_directory / target.name if target.exists() else None
            backups[target] = backup
            if backup is not None:
                shutil.copy2(target, backup)
        for source, target in files:
            os.replace(source, target)
            changed.append(target)
    except OSError as publication_error:
        failures = []
        for target in reversed(changed):
            try:
                backup = backups[target]
                if backup is None:
                    target.unlink(missing_ok=True)
                else:
                    # 원본 백업 자체를 이동하지 않아 다른 파일의 복구 실패에도 보존한다.
                    restored = backup_directory / (target.name + ".restore")
                    shutil.copy2(backup, restored)
                    os.replace(restored, target)
            except OSError as error:
                failures.append(f"{target}: {error}")
        if failures:
            raise OSError(
                "OSNet bundle publication failed and rollback is incomplete; "
                f"original backups retained at {backup_directory}. "
                + "; ".join(failures)
            ) from publication_error
        _discard_bundle_backup(backup_directory)
        raise
    _discard_bundle_backup(backup_directory)


def export_model(
    output: Path, cache: Path, *, offline: bool = False, force: bool = False
) -> dict:
    """PyTorch와 운영 OpenCV 결과를 비교한 파일만 원자적으로 배치한다."""
    import cv2
    import numpy as np
    import onnx
    import torch

    output = output.expanduser().resolve()
    if output.suffix.lower() != ".onnx":
        raise ValueError("Output must have the .onnx extension")
    if output.exists() and not force:
        raise FileExistsError(
            f"Output already exists: {output}; use --force to replace it"
        )
    artifacts = {
        name: prepare_artifact(cache, name, offline=offline) for name in ARTIFACTS
    }
    torch.set_num_threads(1)
    torch.manual_seed(0)
    model = _load_model(artifacts["osnet.py"], artifacts[WEIGHTS_NAME])
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".osnet-export-", dir=output.parent
    ) as temporary:
        temporary_path = Path(temporary) / output.name
        dummy = torch.zeros(1, 3, 256, 128, dtype=torch.float32)
        with torch.inference_mode():
            torch.onnx.export(
                model,
                dummy,
                str(temporary_path),
                opset_version=12,
                input_names=["images"],
                output_names=["embedding"],
                dynamic_axes=None,
                do_constant_folding=True,
                dynamo=False,
            )
        _validate_graph(temporary_path)
        net = cv2.dnn.readNetFromONNX(str(temporary_path))
        net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
        net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        generator = np.random.default_rng(0)
        max_error = 0.0
        # 난수·0·공간적 경사 입력으로 backend 변환의 수치 일치를 확인한다. 정확도 평가는 아니다.
        probes = [
            np.zeros((1, 3, 256, 128), dtype=np.float32),
            generator.standard_normal((1, 3, 256, 128)).astype(np.float32),
            np.linspace(-2, 2, 3 * 256 * 128, dtype=np.float32).reshape(1, 3, 256, 128),
        ]
        for probe in probes:
            with torch.inference_mode():
                reference = model(torch.from_numpy(probe)).numpy()
            net.setInput(probe)
            actual = net.forward()
            if (
                actual.shape != (1, 512)
                or not np.isfinite(actual).all()
                or np.linalg.norm(actual) <= 1e-12
            ):
                raise ValueError(
                    "OpenCV did not produce a finite nonzero 1x512 embedding"
                )
            # 합성곱 누적 순서 차이는 허용하되 실제 비교에 쓰는 단위 벡터도 별도로 검사한다.
            np.testing.assert_allclose(actual, reference, rtol=1e-3, atol=1e-3)
            np.testing.assert_allclose(
                actual / np.linalg.norm(actual),
                reference / np.linalg.norm(reference),
                rtol=1e-3,
                atol=1e-4,
            )
            max_error = max(max_error, float(np.max(np.abs(actual - reference))))
        manifest = {
            "schema_version": 1,
            "architecture": "osnet_x0_25",
            "training_dataset": "MSMT17 combineall",
            "source_revision": SOURCE_REVISION,
            "source_url": ARTIFACTS["osnet.py"][0],
            "weights_url": ARTIFACTS[WEIGHTS_NAME][0],
            "weights_sha256": ARTIFACTS[WEIGHTS_NAME][1],
            "onnx_sha256": hashlib.sha256(temporary_path.read_bytes()).hexdigest(),
            "preprocessing": "rgb256x128-imagenet-v1",
            "input_shape": [1, 3, 256, 128],
            "output_shape": [1, 512],
            "opset": 12,
            "versions": {
                "torch": torch.__version__,
                "onnx": onnx.__version__,
                "opencv": cv2.__version__,
            },
            "validation": {
                "probes": len(probes),
                "max_absolute_error": max_error,
                "reid_accuracy_measured": False,
            },
        }
        manifest_path = Path(temporary) / (output.name + ".json")
        manifest_path.write_text(
            json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
        )
        license_path = temporary_path.with_suffix(".LICENSE.txt")
        license_path.write_bytes(
            _verified_bytes(artifacts["LICENSE"], *ARTIFACTS["LICENSE"][1:])
        )
        # 부속 파일까지 완성한 뒤 게시하며 검증 실패는 기존 모델에 영향을 주지 않는다.
        files = [
            (temporary_path, output),
            (manifest_path, output.with_suffix(".onnx.json")),
            (license_path, output.with_suffix(".LICENSE.txt")),
        ]
        for source, _target in files:
            os.chmod(source, 0o644)
        _publish_bundle(files)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=SERVER_ROOT / "runtime/models" / MODEL_NAME
    )
    parser.add_argument(
        "--cache-dir", type=Path, default=SERVER_ROOT / "runtime/osnet-cache"
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="Use only checksum-verified cached inputs",
    )
    parser.add_argument(
        "--force", action="store_true", help="Replace an existing ONNX after validation"
    )
    args = parser.parse_args()
    try:
        manifest = export_model(
            args.output, args.cache_dir, offline=args.offline, force=args.force
        )
    except (OSError, ValueError, RuntimeError, ImportError, AssertionError) as error:
        parser.exit(
            1,
            f"OSNet preparation failed: {error}\nSee server/tools/README.md for dependencies.\n",
        )
    print(json.dumps({"model": str(args.output.resolve()), **manifest}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
