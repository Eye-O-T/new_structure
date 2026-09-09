# Edge 녹화 복구의 다운로드·검증·색인 순서와 재시도 시 기존 파일 보존을 확인한다.
from __future__ import annotations

import hashlib
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from server.services.data.app.workers.recovery import (
    RecoveryCoordinator,
    RecoveryError,
    read_internal_token,
    read_recovery_token,
)


RECOVERY_TOKEN = "r" * 32
INTERNAL_TOKEN = "i" * 32
START = datetime(2026, 8, 22, 8, 0, tzinfo=UTC)
END = datetime(2026, 8, 22, 8, 1, tzinfo=UTC)


# 복구 클라이언트가 검사하는 HTTP 헤더와 스트림 읽기를 메모리 바이트로 제공한다.
class FakeResponse(io.BytesIO):
    def __init__(self, payload: bytes, *, content_type: str = "application/json"):
        super().__init__(payload)
        self.status = 200
        self.headers = {
            "Content-Length": str(len(payload)),
            "Content-Type": content_type,
        }

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False


# Edge manifest·파일과 Data 색인 API를 한 대역에 연결해 요청 순서와 재처리를 관찰한다.
class FakeServices:
    def __init__(self, manifest: dict, files: dict[str, bytes]):
        self.manifest = manifest
        self.files = files
        self.requests = []
        self.indexed: list[dict] = []

    def __call__(self, request, *, timeout: float):
        assert timeout == 30
        self.requests.append(request)
        parsed = urlsplit(request.full_url)
        if parsed.path == "/v1/recovery/manifest":
            return FakeResponse(json.dumps(self.manifest).encode())
        if parsed.path.startswith("/v1/recovery/files/"):
            relative = parsed.path.removeprefix("/v1/recovery/files/")
            return FakeResponse(self.files[relative], content_type="video/mp2t")
        if parsed.path == "/internal/v1/recording-segments":
            payload = json.loads(request.data)
            replay = any(
                previous["idempotency_key"] == payload["idempotency_key"]
                for previous in self.indexed
            )
            self.indexed.append(payload)
            return FakeResponse(json.dumps({"idempotent_replay": replay}).encode())
        raise AssertionError(f"unexpected request: {request.full_url}")


# 파일 크기·SHA-256이 포함된 manifest 항목을 만들고 잘못된 해시도 주입할 수 있게 한다.
def _item(relative_path: str, content: bytes, *, checksum: str | None = None) -> dict:
    return {
        "camera_id": "cam-001",
        "start_time": "2026-08-22T08:00:00Z",
        "end_time": "2026-08-22T08:00:10Z",
        "relative_path": relative_path,
        "size": len(content),
        "sha256": checksum or hashlib.sha256(content).hexdigest(),
    }


# 실제 네트워크 대신 대역을 사용하면서 다운로드 결과는 독립 임시 저장소에 기록한다.
def _coordinator(tmp_path: Path, services: FakeServices) -> RecoveryCoordinator:
    return RecoveryCoordinator(
        edge_base_url="http://edge.test:8002",
        camera_id="cam-001",
        recovery_token=RECOVERY_TOKEN,
        data_base_url="http://data.test/internal/v1",
        internal_token=INTERNAL_TOKEN,
        recordings_root=tmp_path / "recordings",
        open_request=services,
    )


# 각 파일을 검증한 뒤 색인하고, 동일 복구를 반복하면 기존 파일과 멱등 색인을 재사용한다.
def test_recovery_downloads_sequentially_verifies_and_indexes(tmp_path: Path) -> None:
    first_path = "2026/08/22/20260822T080000.000000Z_000000.ts"
    second_path = "2026/08/22/20260822T080000.000000Z_000001.ts"
    first, second = b"first-mpegts", b"second-mpegts"
    manifest = {
        "camera_id": "cam-001",
        "items": [_item(first_path, first), _item(second_path, second)],
    }
    services = FakeServices(manifest, {first_path: first, second_path: second})
    coordinator = _coordinator(tmp_path, services)

    summary = coordinator.recover(START, END)

    assert summary.selected == 2
    assert summary.downloaded == 2
    assert summary.reused == 0
    assert [urlsplit(request.full_url).path for request in services.requests] == [
        "/v1/recovery/manifest",
        f"/v1/recovery/files/{first_path}",
        "/internal/v1/recording-segments",
        f"/v1/recovery/files/{second_path}",
        "/internal/v1/recording-segments",
    ]
    manifest_query = parse_qs(urlsplit(services.requests[0].full_url).query)
    assert manifest_query == {
        "start": ["2026-08-22T08:00:00.000Z"],
        "end": ["2026-08-22T08:01:00.000Z"],
    }
    for request in services.requests:
        if request.full_url.startswith("http://edge.test"):
            assert request.get_header("Authorization") == f"Bearer {RECOVERY_TOKEN}"
            assert request.get_header("X-internal-token") is None
        else:
            assert request.get_header("X-internal-token") == INTERNAL_TOKEN
            assert request.get_header("Authorization") is None

    root = tmp_path / "recordings" / "recovered" / "cam-001"
    assert (root / first_path).read_bytes() == first
    assert (root / second_path).read_bytes() == second
    assert list((tmp_path / "recordings").rglob("*.part")) == []
    assert all(item["source"] == "edge_recovery" for item in services.indexed)
    assert all(item["format"] == "mpegts" for item in services.indexed)
    assert all(
        item["relative_path"].startswith("recovered/cam-001/2026/08/22/")
        for item in services.indexed
    )
    assert all(
        item["idempotency_key"].startswith("edge-recovery:")
        for item in services.indexed
    )

    second_summary = coordinator.recover(START, END)
    assert second_summary.downloaded == 0
    assert second_summary.reused == 2
    assert second_summary.idempotent_replays == 2
    assert sum("/files/" in request.full_url for request in services.requests) == 2


# 손상된 다운로드는 최종 파일이나 DB 색인이 되지 않고 임시 .part 파일도 정리한다.
def test_checksum_failure_never_commits_or_indexes(tmp_path: Path) -> None:
    relative_path = "2026/08/22/20260822T080000.000000Z_000000.ts"
    content = b"tampered"
    manifest = {
        "camera_id": "cam-001",
        "items": [_item(relative_path, content, checksum="0" * 64)],
    }
    services = FakeServices(manifest, {relative_path: content})
    coordinator = _coordinator(tmp_path, services)
    destination = (
        tmp_path / "recordings" / "recovered" / "cam-001" / relative_path
    )

    with pytest.raises(RecoveryError, match="SHA-256"):
        coordinator.recover(START, END)

    assert not destination.exists()
    assert list((tmp_path / "recordings").rglob("*.part")) == []
    assert services.indexed == []


# 이미 색인된 경로의 내용과 새 manifest가 다르면 기존 녹화를 덮어쓰지 않고 실패한다.
def test_existing_recovery_path_is_immutable_on_manifest_mismatch(
    tmp_path: Path,
) -> None:
    relative_path = "2026/08/22/20260822T080000.000000Z_000000.ts"
    replacement = b"different-valid-content"
    services = FakeServices(
        {
            "camera_id": "cam-001",
            "items": [_item(relative_path, replacement)],
        },
        {relative_path: replacement},
    )
    coordinator = _coordinator(tmp_path, services)
    destination = (
        tmp_path / "recordings" / "recovered" / "cam-001" / relative_path
    )
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"previous-indexed-content")

    with pytest.raises(RecoveryError, match="does not match manifest"):
        coordinator.recover(START, END)

    assert destination.read_bytes() == b"previous-indexed-content"
    assert services.indexed == []
    assert not any("/files/" in request.full_url for request in services.requests)


# 상위 경로 이동·플랫폼 구분자 우회·디렉터리와 파일 날짜 불일치를 다운로드 전에 거부한다.
@pytest.mark.parametrize(
    "relative_path",
    [
        "../../outside.ts",
        "2026/08/22/../../outside.ts",
        "2026\\08\\22\\outside.ts",
        "2026/13/22/20261322T080000Z_000000.ts",
        "2026/08/22/20260823T080000Z_000000.ts",
    ],
)
def test_manifest_path_traversal_and_invalid_date_paths_are_rejected(
    tmp_path: Path,
    relative_path: str,
) -> None:
    content = b"mpegts"
    manifest = {
        "camera_id": "cam-001",
        "items": [_item(relative_path, content)],
    }
    services = FakeServices(manifest, {relative_path: content})

    with pytest.raises(RecoveryError, match="path"):
        _coordinator(tmp_path, services).recover(START, END)

    assert services.indexed == []
    assert not (tmp_path / "outside.ts").exists()


# 복구 토큰은 환경변수 또는 파일 중 한 출처만 사용해 모호한 설정을 막는다.
def test_recovery_token_is_read_from_environment_or_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_file = tmp_path / "recovery.token"
    token_file.write_text(RECOVERY_TOKEN + "\n", encoding="utf-8")
    monkeypatch.delenv("EDGE_RECOVERY_TOKEN", raising=False)
    monkeypatch.setenv("EDGE_RECOVERY_TOKEN_FILE", str(token_file))
    assert read_recovery_token() == RECOVERY_TOKEN

    monkeypatch.setenv("EDGE_RECOVERY_TOKEN", RECOVERY_TOKEN)
    with pytest.raises(RecoveryError, match="only one"):
        read_recovery_token()


# 복구 전용 토큰을 우선 사용하고 없는 경우에만 옛 공용 내부 토큰으로 호환한다.
def test_data_recovery_token_precedes_legacy_internal_token(monkeypatch) -> None:
    monkeypatch.setenv("DATA_RECOVERY_TOKEN", "d" * 32)
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", INTERNAL_TOKEN)
    assert read_internal_token() == "d" * 32

    monkeypatch.delenv("DATA_RECOVERY_TOKEN")
    assert read_internal_token() == INTERNAL_TOKEN


# Linux 훅의 curl을 함수로 대체해 전송 없이 숫자 초 단위 값의 허용·거부를 확인한다.
@pytest.mark.parametrize("duration", ["10", "60.125", "0.000001", "10s", "-1", "nan"])
def test_recording_hook_accepts_mediamtx_numeric_seconds(duration):
    import os
    import shutil
    import subprocess

    if os.name == "nt" or not shutil.which("sh") or not shutil.which("awk"):
        pytest.skip("MediaMTX 셸 계약은 Linux 테스트 컨테이너에서 검증한다")
    result = subprocess.run(
        [
            "sh", "-c",
            'curl() { printf "%s\\n" "$@"; }\n'
            '. server/services/mediamtx/recording-complete-hook.sh',
        ],
        env={
            **os.environ,
            "MTX_PATH": "cam-001",
            "MTX_SEGMENT_PATH": "/recordings/cam-001/test.mp4",
            "MTX_SEGMENT_DURATION": duration,
            "DATA_MEDIA_TOKEN": "test-recording-hook-token-00000000000000",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    if duration in {"10s", "-1", "nan"}:
        assert result.returncode == 2
        assert not result.stdout
    else:
        assert result.returncode == 0, result.stderr
        assert f"duration_seconds={duration}" in result.stdout.splitlines()
