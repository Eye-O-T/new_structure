from __future__ import annotations

import hashlib
import io
import json
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from server.services.data.app.recovery_coordinator import (
    RecoveryCoordinator,
    RecoveryError,
    read_recovery_token,
)


RECOVERY_TOKEN = "r" * 32
INTERNAL_TOKEN = "i" * 32
START = datetime(2026, 8, 22, 8, 0, tzinfo=UTC)
END = datetime(2026, 8, 22, 8, 1, tzinfo=UTC)


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


def _item(relative_path: str, content: bytes, *, checksum: str | None = None) -> dict:
    return {
        "camera_id": "cam-001",
        "start_time": "2026-08-22T08:00:00Z",
        "end_time": "2026-08-22T08:00:10Z",
        "relative_path": relative_path,
        "size": len(content),
        "sha256": checksum or hashlib.sha256(content).hexdigest(),
    }


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

    root = tmp_path / "recordings" / "cam-001"
    assert (root / first_path).read_bytes() == first
    assert (root / second_path).read_bytes() == second
    assert list((tmp_path / "recordings").rglob("*.part")) == []
    assert all(item["source"] == "edge_recovery" for item in services.indexed)
    assert all(item["format"] == "mpegts" for item in services.indexed)
    assert all(
        item["relative_path"].startswith("cam-001/2026/08/22/")
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


def test_checksum_failure_never_commits_or_indexes(tmp_path: Path) -> None:
    relative_path = "2026/08/22/20260822T080000.000000Z_000000.ts"
    content = b"tampered"
    manifest = {
        "camera_id": "cam-001",
        "items": [_item(relative_path, content, checksum="0" * 64)],
    }
    services = FakeServices(manifest, {relative_path: content})
    coordinator = _coordinator(tmp_path, services)
    destination = tmp_path / "recordings" / "cam-001" / relative_path
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"previous-valid-file")

    with pytest.raises(RecoveryError, match="SHA-256"):
        coordinator.recover(START, END)

    assert destination.read_bytes() == b"previous-valid-file"
    assert list((tmp_path / "recordings").rglob("*.part")) == []
    assert services.indexed == []


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
