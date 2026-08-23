"""One-shot central coordinator for importing Edge recovery segments.

Run this module inside the existing Data container.  It intentionally does not
start a daemon: an operator or scheduler supplies one explicit UTC interval,
the coordinator fetches the Edge manifest, commits each verified file, and
registers it through the Data Service's authenticated HTTP API.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import re
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from ai_cctv_core.identifiers import safe_storage_path, validate_camera_id
from ai_cctv_core.time import format_utc, parse_utc


MAX_MANIFEST_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_SEGMENT_BYTES = 512 * 1024 * 1024
SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
EDGE_PATH_PATTERN = re.compile(
    r"^(?P<year>\d{4})/(?P<month>\d{2})/(?P<day>\d{2})/"
    r"(?P<stamp>\d{8})T\d{6}(?:\.\d+)?Z_\d{6}\.ts$"
)


class RecoveryError(RuntimeError):
    """A safe, operator-facing recovery failure."""


class _RejectRedirects(HTTPRedirectHandler):
    """Do not forward either service credential across an HTTP redirect."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        del req, fp, code, msg, headers, newurl
        return None


# Internal bearer/service credentials must never be handed to a host-configured
# HTTP(S) proxy. Edge and loopback destinations are selected explicitly.
_HTTP_OPENER = build_opener(ProxyHandler({}), _RejectRedirects())


@dataclass(frozen=True)
class ManifestItem:
    camera_id: str
    start_time: datetime
    end_time: datetime
    relative_path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class RecoverySummary:
    camera_id: str
    selected: int
    downloaded: int
    reused: int
    indexed: int
    idempotent_replays: int


def _default_open(request: Request, *, timeout: float):
    return _HTTP_OPENER.open(request, timeout=timeout)


def _validated_base_url(value: str, label: str) -> str:
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
    except (TypeError, ValueError) as exc:
        raise RecoveryError(f"{label} must be an HTTP(S) URL") from exc
    if parsed.scheme not in {"http", "https"} or not hostname:
        raise RecoveryError(f"{label} must be an HTTP(S) URL")
    if parsed.username is not None or parsed.password is not None:
        raise RecoveryError(f"{label} must not contain credentials")
    if parsed.query or parsed.fragment:
        raise RecoveryError(f"{label} must not contain a query or fragment")
    if any(part == ".." for part in parsed.path.split("/")):
        raise RecoveryError(f"{label} contains an invalid path")
    return value.rstrip("/")


def _read_secret(
    value_environment: str,
    file_environment: str,
    *,
    minimum_length: int,
) -> str:
    direct = os.getenv(value_environment)
    filename = os.getenv(file_environment)
    if direct is not None and filename is not None:
        raise RecoveryError(
            f"set only one of {value_environment} or {file_environment}"
        )
    if direct is None and filename is None:
        raise RecoveryError(f"set {value_environment} or {file_environment}")

    if filename is not None:
        try:
            secret_path = Path(filename).expanduser()
            if not secret_path.is_file():
                raise OSError
            direct = secret_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise RecoveryError(f"could not read {file_environment}") from exc

    value = (direct or "").strip()
    if len(value) < minimum_length or "\x00" in value:
        raise RecoveryError(f"{value_environment} is invalid")
    return value


def read_recovery_token() -> str:
    return _read_secret(
        "EDGE_RECOVERY_TOKEN",
        "EDGE_RECOVERY_TOKEN_FILE",
        minimum_length=32,
    )


def read_internal_token() -> str:
    if os.getenv("DATA_RECOVERY_TOKEN") or os.getenv("DATA_RECOVERY_TOKEN_FILE"):
        return _read_secret(
            "DATA_RECOVERY_TOKEN",
            "DATA_RECOVERY_TOKEN_FILE",
            minimum_length=32,
        )
    return _read_secret(
        "INTERNAL_SERVICE_TOKEN",
        "INTERNAL_SERVICE_TOKEN_FILE",
        minimum_length=16,
    )


def _positive_integer_environment(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise RecoveryError(f"{name} must be an integer") from exc
    if value <= 0:
        raise RecoveryError(f"{name} must be greater than zero")
    return value


def _manifest_relative_path(raw_path: Any) -> str:
    if not isinstance(raw_path, str):
        raise RecoveryError("Edge manifest contains an invalid relative path")
    match = EDGE_PATH_PATTERN.fullmatch(raw_path)
    if match is None:
        raise RecoveryError("Edge manifest contains an invalid relative path")
    try:
        date(
            int(match.group("year")),
            int(match.group("month")),
            int(match.group("day")),
        )
    except ValueError as exc:
        raise RecoveryError("Edge manifest contains an invalid date path") from exc
    expected_stamp = match.group("year") + match.group("month") + match.group("day")
    if match.group("stamp") != expected_stamp:
        raise RecoveryError("Edge manifest date path does not match its filename")
    return PurePosixPath(raw_path).as_posix()


def _parse_manifest(
    payload: Any,
    *,
    expected_camera_id: str,
    requested_start: datetime,
    requested_end: datetime,
    max_segment_bytes: int,
) -> list[ManifestItem]:
    if not isinstance(payload, dict):
        raise RecoveryError("Edge manifest response is invalid")
    if payload.get("camera_id") != expected_camera_id:
        raise RecoveryError("Edge manifest camera does not match the request")
    raw_items = payload.get("items")
    if not isinstance(raw_items, list):
        raise RecoveryError("Edge manifest response is invalid")

    items: list[ManifestItem] = []
    seen_paths: set[str] = set()
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            raise RecoveryError("Edge manifest item is invalid")
        if raw_item.get("camera_id") != expected_camera_id:
            raise RecoveryError("Edge manifest item has a different camera")
        relative_path = _manifest_relative_path(raw_item.get("relative_path"))
        if relative_path in seen_paths:
            raise RecoveryError("Edge manifest contains a duplicate path")
        seen_paths.add(relative_path)

        try:
            start_time = parse_utc(raw_item["start_time"])
            end_time = parse_utc(raw_item["end_time"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RecoveryError("Edge manifest item has invalid timestamps") from exc
        if start_time >= end_time:
            raise RecoveryError("Edge manifest item has an invalid time range")
        if not (start_time < requested_end and end_time > requested_start):
            raise RecoveryError("Edge manifest returned an item outside the request")

        size = raw_item.get("size")
        if isinstance(size, bool) or not isinstance(size, int):
            raise RecoveryError("Edge manifest item has an invalid size")
        if size <= 0 or size > max_segment_bytes:
            raise RecoveryError("Edge manifest item size is outside the allowed range")

        checksum = raw_item.get("sha256")
        if not isinstance(checksum, str) or not SHA256_PATTERN.fullmatch(checksum):
            raise RecoveryError("Edge manifest item has an invalid SHA-256")
        items.append(
            ManifestItem(
                camera_id=expected_camera_id,
                start_time=start_time,
                end_time=end_time,
                relative_path=relative_path,
                size=size,
                sha256=checksum.lower(),
            )
        )
    return items


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _idempotency_key(item: ManifestItem, central_relative_path: str) -> str:
    identity = "\x00".join(
        (
            item.camera_id,
            format_utc(item.start_time),
            format_utc(item.end_time),
            central_relative_path,
            str(item.size),
            item.sha256,
        )
    )
    return "edge-recovery:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


class RecoveryCoordinator:
    def __init__(
        self,
        *,
        edge_base_url: str,
        camera_id: str,
        recovery_token: str,
        data_base_url: str,
        internal_token: str,
        recordings_root: str | Path,
        timeout_seconds: float = 30.0,
        max_segment_bytes: int = DEFAULT_MAX_SEGMENT_BYTES,
        open_request: Callable[..., Any] | None = None,
        progress_callback: Callable[[str], None] | None = None,
    ) -> None:
        try:
            self.camera_id = validate_camera_id(camera_id)
        except ValueError as exc:
            raise RecoveryError("camera_id is invalid") from exc
        if len(recovery_token) < 32 or len(internal_token) < 16:
            raise RecoveryError("service credential is invalid")
        if (
            not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
            or max_segment_bytes <= 0
        ):
            raise RecoveryError("timeout and segment limit must be greater than zero")

        self.edge_base_url = _validated_base_url(edge_base_url, "Edge base URL")
        self.data_base_url = _validated_base_url(data_base_url, "Data base URL")
        self.recovery_token = recovery_token
        self.internal_token = internal_token
        self.recordings_root = Path(recordings_root).expanduser().resolve()
        self.timeout_seconds = timeout_seconds
        self.max_segment_bytes = max_segment_bytes
        self._open_request = open_request or _default_open
        self._progress_callback = progress_callback

    def _read_json(self, request: Request, service: str) -> Any:
        try:
            with self._open_request(
                request,
                timeout=self.timeout_seconds,
            ) as response:
                status = getattr(response, "status", response.getcode())
                if not 200 <= status < 300:
                    raise RecoveryError(f"{service} request was rejected")
                raw = response.read(MAX_MANIFEST_BYTES + 1)
        except RecoveryError:
            raise
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            raise RecoveryError(f"{service} request failed") from exc
        if len(raw) > MAX_MANIFEST_BYTES:
            raise RecoveryError(f"{service} response is too large")
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RecoveryError(f"{service} returned invalid JSON") from exc

    def _manifest(
        self,
        requested_start: datetime,
        requested_end: datetime,
    ) -> list[ManifestItem]:
        query = urlencode(
            {
                "start": format_utc(requested_start),
                "end": format_utc(requested_end),
            }
        )
        request = Request(
            f"{self.edge_base_url}/v1/recovery/manifest?{query}",
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {self.recovery_token}",
            },
            method="GET",
        )
        return _parse_manifest(
            self._read_json(request, "Edge manifest"),
            expected_camera_id=self.camera_id,
            requested_start=requested_start,
            requested_end=requested_end,
            max_segment_bytes=self.max_segment_bytes,
        )

    def _destination(self, item: ManifestItem) -> tuple[str, Path]:
        central_relative = (
            PurePosixPath("recovered")
            / PurePosixPath(self.camera_id)
            / PurePosixPath(item.relative_path)
        ).as_posix()
        try:
            destination = safe_storage_path(self.recordings_root, central_relative)
        except ValueError as exc:
            raise RecoveryError("recovery path escapes the recordings root") from exc
        return central_relative, destination

    def _download(self, item: ManifestItem, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        request = Request(
            (
                f"{self.edge_base_url}/v1/recovery/files/"
                f"{quote(item.relative_path, safe='/')}"
            ),
            headers={"Authorization": f"Bearer {self.recovery_token}"},
            method="GET",
        )
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".part",
            dir=destination.parent,
        )
        temporary = Path(temporary_name)
        try:
            digest = hashlib.sha256()
            total = 0
            try:
                with os.fdopen(descriptor, "wb") as output:
                    with self._open_request(
                        request,
                        timeout=self.timeout_seconds,
                    ) as response:
                        status = getattr(response, "status", response.getcode())
                        if not 200 <= status < 300:
                            raise RecoveryError("Edge file request was rejected")
                        content_length = response.headers.get("Content-Length")
                        if content_length is not None:
                            try:
                                declared_length = int(content_length)
                            except ValueError as exc:
                                raise RecoveryError(
                                    "Edge file has an invalid Content-Length"
                                ) from exc
                            if declared_length != item.size:
                                raise RecoveryError(
                                    "Edge file size does not match manifest"
                                )
                        while True:
                            chunk = response.read(1024 * 1024)
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > item.size:
                                raise RecoveryError(
                                    "Edge file size does not match manifest"
                                )
                            output.write(chunk)
                            digest.update(chunk)
                    output.flush()
                    os.fsync(output.fileno())
            except RecoveryError:
                raise
            except (HTTPError, URLError, OSError, TimeoutError) as exc:
                raise RecoveryError("Edge file download failed") from exc

            if total != item.size:
                raise RecoveryError("Edge file size does not match manifest")
            if not hmac.compare_digest(digest.hexdigest(), item.sha256):
                raise RecoveryError("Edge file SHA-256 verification failed")
            os.chmod(temporary, 0o640)
            os.replace(temporary, destination)
            # POSIX directory fsync makes the atomic rename durable. Windows
            # does not permit opening a directory with os.open; os.replace is
            # still atomic there and the file itself was flushed above.
            if os.name != "nt":
                directory_descriptor = os.open(
                    destination.parent,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                )
                try:
                    os.fsync(directory_descriptor)
                finally:
                    os.close(directory_descriptor)
        finally:
            temporary.unlink(missing_ok=True)

    def _index(self, item: ManifestItem, central_relative_path: str) -> Any:
        payload = {
            "camera_id": item.camera_id,
            "start_time": format_utc(item.start_time),
            "end_time": format_utc(item.end_time),
            "relative_path": central_relative_path,
            "format": "mpegts",
            "codec": "h264",
            "duration_ms": round(
                (item.end_time - item.start_time).total_seconds() * 1000
            ),
            "file_size": item.size,
            "source": "edge_recovery",
            "status": "ready",
            "checksum": item.sha256,
            "idempotency_key": _idempotency_key(item, central_relative_path),
        }
        request = Request(
            f"{self.data_base_url}/recording-segments",
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "X-Internal-Token": self.internal_token,
            },
            method="POST",
        )
        return self._read_json(request, "Data indexing")

    def recover(self, start: str | datetime, end: str | datetime) -> RecoverySummary:
        try:
            requested_start = parse_utc(start)
            requested_end = parse_utc(end)
        except (TypeError, ValueError) as exc:
            raise RecoveryError(
                "start and end must be timezone-aware timestamps"
            ) from exc
        if requested_start >= requested_end:
            raise RecoveryError("start must be earlier than end")
        if requested_end - requested_start > timedelta(hours=24):
            raise RecoveryError("recovery range cannot exceed 24 hours")

        items = self._manifest(requested_start, requested_end)
        downloaded = 0
        reused = 0
        idempotent_replays = 0
        indexing_reported = False
        for item in items:
            central_relative, destination = self._destination(item)
            if destination.exists():
                if not destination.is_file():
                    raise RecoveryError(
                        "existing recovery destination is not a regular file"
                    )
                existing_matches = (
                    destination.stat().st_size == item.size
                    and _sha256_file(destination) == item.sha256
                )
                if not existing_matches:
                    # Relative paths are immutable identities. Replacing a
                    # different file would leave an existing database row
                    # describing bytes that are no longer on disk.
                    raise RecoveryError(
                        "existing recovery destination does not match manifest"
                    )
                reused += 1
            else:
                self._download(item, destination)
                downloaded += 1
            if not indexing_reported and self._progress_callback is not None:
                self._progress_callback("indexing")
                indexing_reported = True
            indexed = self._index(item, central_relative)
            if isinstance(indexed, dict) and indexed.get("idempotent_replay") is True:
                idempotent_replays += 1

        return RecoverySummary(
            camera_id=self.camera_id,
            selected=len(items),
            downloaded=downloaded,
            reused=reused,
            indexed=len(items),
            idempotent_replays=idempotent_replays,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-cctv-edge-recover",
        description="Import one explicit Edge recovery interval into central storage.",
    )
    parser.add_argument("--edge-url", default=os.getenv("EDGE_RECOVERY_URL"))
    parser.add_argument("--camera-id", default=os.getenv("EDGE_CAMERA_ID"))
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument(
        "--data-url",
        default=os.getenv(
            "DATA_INTERNAL_BASE_URL",
            "http://127.0.0.1:8000/internal/v1",
        ),
    )
    parser.add_argument(
        "--recordings-root",
        default=(
            os.getenv("RECORDINGS_ROOT")
            or os.getenv("DATA_STORAGE_ROOT")
            or "/data/recordings"
        ),
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.edge_url:
        parser.error("--edge-url or EDGE_RECOVERY_URL is required")
    if not args.camera_id:
        parser.error("--camera-id or EDGE_CAMERA_ID is required")

    try:
        coordinator = RecoveryCoordinator(
            edge_base_url=args.edge_url,
            camera_id=args.camera_id,
            recovery_token=read_recovery_token(),
            data_base_url=args.data_url,
            internal_token=read_internal_token(),
            recordings_root=args.recordings_root,
            timeout_seconds=float(os.getenv("EDGE_RECOVERY_TIMEOUT_SECONDS", "30")),
            max_segment_bytes=_positive_integer_environment(
                "EDGE_RECOVERY_MAX_SEGMENT_BYTES",
                DEFAULT_MAX_SEGMENT_BYTES,
            ),
        )
        summary = coordinator.recover(args.start, args.end)
    except (RecoveryError, OSError, ValueError) as exc:
        message = (
            str(exc) if isinstance(exc, RecoveryError) else "local operation failed"
        )
        print(f"recovery failed: {message}", file=sys.stderr)
        return 1

    print(json.dumps(asdict(summary), separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
