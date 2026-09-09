"""네트워크 없이 모델 준비의 무결성·중단 복구 경계를 검증한다."""

import hashlib
import io
from contextlib import nullcontext
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from server.tools import prepare_osnet as preparation


@pytest.fixture
def artifact(monkeypatch):
    payload = b"test checkpoint bytes"
    monkeypatch.setattr(
        preparation,
        "ARTIFACTS",
        {
            "weights.pth": (
                "https://example.invalid/weights",
                hashlib.sha256(payload).hexdigest(),
                64,
            ),
        },
    )
    return payload


def test_offline_cache_is_verified_and_never_downloaded(
    tmp_path, monkeypatch, artifact
):
    target = tmp_path / "weights.pth"
    target.write_bytes(artifact)

    def no_network(*args, **kwargs):
        pytest.fail("offline cache must not access the network")

    monkeypatch.setattr(preparation.urllib.request, "urlopen", no_network)
    assert preparation.prepare_artifact(tmp_path, target.name, offline=True) == target
    target.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="SHA-256"):
        preparation.prepare_artifact(tmp_path, target.name, offline=True)


def test_missing_offline_cache_is_actionable(tmp_path, artifact):
    with pytest.raises(FileNotFoundError, match="Offline cache is missing"):
        preparation.prepare_artifact(tmp_path, "weights.pth", offline=True)


class Response(io.BytesIO):
    def geturl(self):
        return "https://example.invalid/weights"


@pytest.mark.parametrize("failure", ["size", "checksum", "interrupted"])
def test_failed_download_does_not_leave_a_usable_cache(
    tmp_path, monkeypatch, artifact, failure
):
    response = Response(b"x" * 65 if failure == "size" else b"wrong checkpoint")
    if failure == "interrupted":

        def interrupted(size):
            raise OSError("connection interrupted")

        response.read = interrupted
    monkeypatch.setattr(preparation.urllib.request, "urlopen", lambda *a, **k: response)
    with pytest.raises((ValueError, OSError)):
        preparation.prepare_artifact(tmp_path, "weights.pth", offline=False)
    assert list(tmp_path.iterdir()) == []


def test_successful_download_becomes_verified_offline_cache(
    tmp_path, monkeypatch, artifact
):
    monkeypatch.setattr(
        preparation.urllib.request, "urlopen", lambda *a, **k: Response(artifact)
    )
    path = preparation.prepare_artifact(tmp_path, "weights.pth", offline=False)
    assert path.read_bytes() == artifact
    assert preparation.prepare_artifact(tmp_path, "weights.pth", offline=True) == path


def test_graph_rejects_classifier_output_and_accepts_embedding(tmp_path):
    onnx = pytest.importorskip("onnx")
    for dimensions in (512, 1000):
        inputs = onnx.helper.make_tensor_value_info(
            "images", onnx.TensorProto.FLOAT, [1, 3, 256, 128]
        )
        outputs = onnx.helper.make_tensor_value_info(
            "embedding", onnx.TensorProto.FLOAT, [1, dimensions]
        )
        tensor = onnx.helper.make_tensor(
            "value", onnx.TensorProto.FLOAT, [1, dimensions], [1.0] * dimensions
        )
        graph = onnx.helper.make_graph(
            [
                onnx.helper.make_node("Constant", [], ["embedding"], value=tensor),
            ],
            "fixture",
            [inputs],
            [outputs],
        )
        model = onnx.helper.make_model(
            graph, opset_imports=[onnx.helper.make_opsetid("", 12)]
        )
        path = tmp_path / "fixture.onnx"
        onnx.save(model, path)
        if dimensions == 512:
            preparation._validate_graph(path)
        else:
            with pytest.raises(ValueError, match="tensor contract"):
                preparation._validate_graph(path)


def _bundle(tmp_path, *, existing=True):
    """서로 다른 세 파일로 원본·새 번들을 구성해 일부만 교체된 상황을 구별한다."""
    staged = tmp_path / "staged"
    staged.mkdir()
    files = []
    for index, name in enumerate(("model.onnx", "model.onnx.json", "model.LICENSE.txt")):
        source, target = staged / name, tmp_path / name
        source.write_bytes(f"new-{index}".encode())
        if existing:
            target.write_bytes(f"old-{index}".encode())
        files.append((source, target))
    return files


def test_bundle_success_publishes_all_files_and_removes_backups(tmp_path):
    files = _bundle(tmp_path)
    preparation._publish_bundle(files)
    assert [target.read_bytes() for _, target in files] == [b"new-0", b"new-1", b"new-2"]
    assert list(tmp_path.glob(".osnet-backup-*")) == []


@pytest.mark.parametrize("existing", [True, False])
@pytest.mark.parametrize("failure_index", [0, 1, 2])
def test_bundle_publication_failure_restores_only_changed_files(
    tmp_path, monkeypatch, existing, failure_index
):
    files = _bundle(tmp_path, existing=existing)
    replace = preparation.os.replace
    restored = []

    def failing_replace(source, target):
        if (source, target) == files[failure_index]:
            raise OSError("publication blocked")
        if source.name.endswith(".restore"):
            restored.append(target)
        replace(source, target)

    monkeypatch.setattr(preparation.os, "replace", failing_replace)
    with pytest.raises(OSError, match="publication blocked"):
        preparation._publish_bundle(files)
    for index, (_, target) in enumerate(files):
        if existing:
            assert target.read_bytes() == f"old-{index}".encode()
        else:
            assert not target.exists()
    assert restored == (
        [target for _, target in reversed(files[:failure_index])] if existing else []
    )
    assert list(tmp_path.glob(".osnet-backup-*")) == []


def test_bundle_backup_failure_never_starts_publication(tmp_path, monkeypatch):
    files = _bundle(tmp_path)
    copy = preparation.shutil.copy2

    def failing_copy(source, target):
        if source == files[1][1]:
            # 불완전 백업도 정식 배포 파일을 건드리지 않고 정리해야 한다.
            target.write_bytes(b"partial backup")
            raise OSError("backup storage full")
        return copy(source, target)

    def no_publication(*args):
        pytest.fail("every original must be backed up before publication")

    monkeypatch.setattr(preparation.shutil, "copy2", failing_copy)
    monkeypatch.setattr(preparation.os, "replace", no_publication)
    with pytest.raises(OSError, match="backup storage full"):
        preparation._publish_bundle(files)
    assert [target.read_bytes() for _, target in files] == [b"old-0", b"old-1", b"old-2"]
    assert list(tmp_path.glob(".osnet-backup-*")) == []


def test_failed_rollback_retains_original_backups_and_reports_their_path(
    tmp_path, monkeypatch
):
    files = _bundle(tmp_path)
    replace = preparation.os.replace

    def failing_replace(source, target):
        if (source, target) == files[2]:
            raise OSError("license publication blocked")
        if target == files[0][1] and source.name.endswith(".restore"):
            raise OSError("model rollback blocked")
        replace(source, target)

    monkeypatch.setattr(preparation.os, "replace", failing_replace)
    with pytest.raises(OSError, match="rollback is incomplete") as failure:
        preparation._publish_bundle(files)
    directories = list(tmp_path.glob(".osnet-backup-*"))
    assert len(directories) == 1
    assert str(directories[0]) in str(failure.value)
    # 복구에 성공한 manifest를 포함하여 세 원본 모두 수동 복구용으로 남긴다.
    for index, (_, target) in enumerate(files):
        assert (directories[0] / target.name).read_bytes() == f"old-{index}".encode()
    assert files[0][1].read_bytes() == b"new-0"
    assert files[1][1].read_bytes() == b"old-1"
    assert files[2][1].read_bytes() == b"old-2"


def test_force_export_validation_failure_keeps_existing_bundle(tmp_path, monkeypatch):
    files = _bundle(tmp_path)
    model = files[0][1]

    def write_invalid_export(_model, _dummy, path, **kwargs):
        Path(path).write_bytes(b"invalid ONNX")

    # 그래프 검증 실패 이후 게시하지 않는 경계를 학습 모델·네트워크 없이 검사한다.
    fake_torch = SimpleNamespace(
        set_num_threads=lambda _: None,
        manual_seed=lambda _: None,
        zeros=lambda *a, **k: None,
        float32="float32",
        inference_mode=nullcontext,
        onnx=SimpleNamespace(export=write_invalid_export),
    )
    for name in ("cv2", "numpy", "onnx"):
        monkeypatch.setitem(sys.modules, name, SimpleNamespace())
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setattr(
        preparation, "prepare_artifact", lambda cache, name, **k: cache / name
    )
    monkeypatch.setattr(preparation, "_load_model", lambda *a: object())

    def reject_graph(path):
        assert path.read_bytes() == b"invalid ONNX"
        raise ValueError("export graph rejected")

    monkeypatch.setattr(preparation, "_validate_graph", reject_graph)
    with pytest.raises(ValueError, match="export graph rejected"):
        preparation.export_model(model, tmp_path / "cache", offline=True, force=True)
    assert [target.read_bytes() for _, target in files] == [b"old-0", b"old-1", b"old-2"]
    assert list(tmp_path.glob(".osnet-backup-*")) == []
    assert list(tmp_path.glob(".osnet-export-*")) == []
