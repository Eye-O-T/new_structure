"""설치가 허용한 모델 형식을 배포 기록에서도 실제 파일 해시로 보존한다."""

import hashlib
import json

import pytest

from server.tools.export_release_manifest import collect


def _docker(arguments):
    if "compose" in arguments:
        return json.dumps([{"Service": "nginx", "ID": "container-id"}])
    if "--format" in arguments:
        return "sha256:image-id"
    return json.dumps([{"Id": "sha256:image-id", "RepoDigests": []}])


@pytest.mark.parametrize("suffix", [".pt", ".onnx", ".engine", ".ENGINE"])
def test_manifest_records_all_installer_model_formats(tmp_path, suffix):
    env = tmp_path / "compose.env"
    env.write_text("SECRET=never-record-this\n", encoding="utf-8")
    model = tmp_path / f"detector{suffix}"
    payload = b"test model artifact\x00\xff"
    model.write_bytes(payload)
    manifest = collect(tmp_path, env, [model], runner=_docker)
    assert manifest["models"] == [
        {
            "filename": model.name,
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }
    ]
    assert "never-record-this" not in json.dumps(manifest)


def test_manifest_rejects_unrecognized_model_format(tmp_path):
    env = tmp_path / "compose.env"
    env.touch()
    model = tmp_path / "model.txt"
    model.write_bytes(b"model")
    with pytest.raises(ValueError, match="existing .pt, .onnx or .engine"):
        collect(tmp_path, env, [model], runner=_docker)
