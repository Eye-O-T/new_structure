# Edge에 남은 녹화를 복구한다. 수동 실행과 백그라운드 작업이 같은 복구 절차를 쓴다.
"""Data 컨테이너에서 Edge 녹화를 검증·저장한 뒤 내부 API로 등록한다.
수동 실행은 지정한 UTC 구간을 한 번 처리하며 별도 데몬을 시작하지 않는다."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import json
import logging
import math
import os
import re
import sys
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from ai_cctv_core.identifiers import safe_storage_path, validate_camera_id
from ai_cctv_core.time import format_utc, parse_utc, utc_now

from ..config import Settings
from ..database.repositories import DataRepository
from .supervision import WorkerStatus

MAX_MANIFEST_BYTES = 8 * 1024 * 1024
DEFAULT_MAX_SEGMENT_BYTES = 512 * 1024 * 1024
SHA256_PATTERN = re.compile(r"^[0-9a-fA-F]{64}$")
EDGE_PATH_PATTERN = re.compile(
    r"^(?P<year>\d{4})/(?P<month>\d{2})/(?P<day>\d{2})/"
    r"(?P<stamp>\d{8})T\d{6}(?:\.\d+)?Z_\d{6}\.ts$"
)


class RecoveryError(RuntimeError):
    """인증값 없이 운영자에게 전달할 복구 오류."""


class RecoveryProcessDidNotStop(RecoveryError):
    """종료되지 않은 자식이 있으므로 후속 작업을 실행해서는 안 된다."""


class _RejectRedirects(HTTPRedirectHandler):
    """HTTP 리다이렉트로 내부 인증값이 다른 주소에 전달되지 않게 한다."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        del req, fp, code, msg, headers, newurl
        return None


# 내부 인증값이 호스트의 프록시로 새지 않도록 지정한 Edge·루프백 주소에 직접 접속한다.
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


# 인증·질의·상대 상위 경로가 섞이지 않은 HTTP(S) 서비스 기준 주소만 허용한다.
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


# 환경 변수와 파일 중 하나만 허용하며 비밀 원문을 오류 메시지에 포함하지 않는다.
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


# 복구 전용 토큰 설정을 우선하고 없는 경우에만 구형 공통 토큰을 사용한다.
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


# Edge 녹화 파일 규칙과 실제 달력 날짜·파일명 날짜의 일치를 함께 검사한다.
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


# 카메라·시간 겹침·경로·크기·해시 계약을 통과한 목록만 다운로드 대상으로 변환한다.
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


# 대용량 영상을 한 번에 메모리에 올리지 않고 청크 단위로 내용 지문을 계산한다.
def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# 카메라·시간·경로·크기·해시가 같은 복구 요청에 항상 같은 등록 키를 생성한다.
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


# 신뢰하지 않는 Edge 목록을 검증한 뒤 파일 준비와 Data 등록을 순서대로 실행한다.
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
        temporary_callback: Callable[[Path | None], None] | None = None,
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
        self._temporary_callback = temporary_callback

    # 응답 크기를 제한하고 네트워크·JSON 오류를 인증 정보 없는 복구 오류로 바꾼다.
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

    # UTC 구간을 명시한 목록을 요청하고 반환 항목의 카메라와 파일 계약을 재검증한다.
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

    # 중앙 녹화와 충돌하지 않도록 recovered/카메라 경로 아래에 파일을 배치한다.
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

    # 임시 파일로 내려받아 크기·해시를 확인한 뒤 원자적으로 최종 경로에 배치한다.
    def _download(self, item: ManifestItem, destination: Path) -> None:
        # 내려받는 중인 파일은 .part로 분리해 불완전한 영상이 재생 목록에 나타나지 않게 한다.
        destination.parent.mkdir(parents=True, exist_ok=True)
        request = Request(
            (
                f"{self.edge_base_url}/v1/recovery/files/"
                f"{quote(item.relative_path, safe='/')}"
            ),
            headers={"Authorization": f"Bearer {self.recovery_token}"},
            method="GET",
        )
        temporary = destination.parent / (".recovery-" + uuid.uuid4().hex + ".part")
        # 부모가 경로를 전달받은 뒤에만 파일을 만든다. 생성 직후 강제 종료되는 틈도 정리한다.
        if self._temporary_callback is not None:
            self._temporary_callback(temporary)
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
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
            # 크기뿐 아니라 내용의 지문(SHA-256)도 맞아야 정상 파일 이름으로 교체한다.
            if not hmac.compare_digest(digest.hexdigest(), item.sha256):
                raise RecoveryError("Edge file SHA-256 verification failed")
            os.chmod(temporary, 0o640)
            os.replace(temporary, destination)
            # POSIX는 폴더도 fsync해 이름 변경을 디스크에 확정한다. Windows는 파일 동기화와
            # os.replace의 원자성을 사용하며 폴더를 os.open으로 열 수 없다.
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
            if self._temporary_callback is not None:
                self._temporary_callback(None)

    # 검증된 복구 파일을 MPEG-TS 녹화로 등록하고 재실행에도 같은 멱등 키를 보낸다.
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

    # 24시간 이하의 구간을 복구하며 일치하는 기존 파일은 재사용하고 내용 충돌은 거부한다.
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
                    # 경로는 녹화의 고정 식별자다. 다른 내용으로 덮으면 기존 DB 정보와 파일이 어긋난다.
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
            # 검증된 파일이 준비된 뒤 DB에 등록한다. 같은 키로 재요청해도 중복 등록되지 않는다.
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


# 한 번의 수동 복구에 필요한 Edge·Data 주소와 명시적 UTC 구간을 입력받는다.
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


# 수동 복구의 요약을 JSON으로 출력하고 예상 실패는 비밀을 제외한 메시지와 종료 코드로 알린다.
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


LOGGER = logging.getLogger("ai_cctv.data")


# 장애 구간을 24시간씩 나누어 처리하고 원래 revision에 대해서만 완료 또는 지수 재시도를 기록한다.
def execute_recovery(
    job: dict[str, Any],
    repository: DataRepository,
    settings: Settings,
    *,
    stop_event: threading.Event | None = None,
) -> None:
    from .recovery_execution import run_recovery_job

    job_id = int(job["id"])
    revision = int(job.get("revision", 0))

    def progress(stage: str) -> None:
        if stage == "indexing":
            repository.update_recovery_job(
                job_id,
                status="indexing",
                expected_revision=revision,
            )

    try:
        aggregate = run_recovery_job(job, settings, progress, stop_event=stop_event)
    except RecoveryProcessDidNotStop:
        raise
    except RecoveryError as exc:
        attempt = int(job["attempt_count"])
        retry_at = None
        if attempt < int(job["max_attempts"]):
            delay = settings.recovery_retry_base_seconds * (2 ** max(0, attempt - 1))
            retry_at = format_utc(utc_now() + timedelta(seconds=delay))
        repository.update_recovery_job(
            job_id,
            status="failed",
            last_error=str(exc)[:1024],
            next_retry_at=retry_at,
            expected_revision=revision,
        )
        return
    repository.update_recovery_job(
        job_id,
        status="completed",
        recovery_summary=aggregate,
        expected_revision=revision,
    )


# DB에서 작업을 하나씩 가져와 블로킹 복구를 스레드로 실행하고 예기치 않은 실패도 재예약한다.
async def recover_outages(
    repository: DataRepository, settings: Settings, state: WorkerStatus | None = None
) -> None:
    # 복구 결과는 자체 내부 API로 등록한다. 시작 직후에는 HTTP 서버가 열릴 시간을 준다.
    await asyncio.sleep(settings.recovery_poll_interval_seconds)
    while True:
        try:
            job = await asyncio.to_thread(repository.claim_due_recovery_job)
        except Exception as exc:
            if state is not None:
                state.failed(exc)
            LOGGER.exception("automatic Edge recovery claim failed; retrying")
            await asyncio.sleep(settings.recovery_poll_interval_seconds)
            continue
        if job is None:
            if state is not None:
                state.succeeded()
            await asyncio.sleep(settings.recovery_poll_interval_seconds)
            continue
        try:
            stop = threading.Event()
            execution = asyncio.create_task(
                asyncio.to_thread(
                    execute_recovery, job, repository, settings, stop_event=stop
                )
            )
            try:
                await asyncio.shield(execution)
            except asyncio.CancelledError:
                stop.set()
                await asyncio.gather(execution, return_exceptions=True)
                raise
        except RecoveryProcessDidNotStop as exc:
            if state is not None:
                state.failed(exc)
                state.last_error = "RECOVERY_PROCESS_DID_NOT_STOP"
            LOGGER.critical(
                "recovery child could not be stopped; further claims are blocked"
            )
            # 살아 있는 이전 자식과 후속 작업이 같은 파일을 쓰지 않게 운영자 복구까지 멈춘다.
            await asyncio.Event().wait()
        except Exception as exc:
            if state is not None:
                state.failed(exc)
            LOGGER.exception("automatic Edge recovery failed unexpectedly")
            attempt = int(job["attempt_count"])
            retry_at = None
            if attempt < int(job["max_attempts"]):
                delay = settings.recovery_retry_base_seconds * (
                    2 ** max(0, attempt - 1)
                )
                retry_at = format_utc(utc_now() + timedelta(seconds=delay))
            # 실패 상태를 저장하지 못하면 다음 작업을 가져가지 않는다. DB가 회복될 때까지
            # 같은 revision의 실패 기록을 재시도하여 downloading 상태가 영구히 남지 않게 한다.
            while True:
                try:
                    await asyncio.to_thread(
                        repository.update_recovery_job,
                        int(job["id"]),
                        status="failed",
                        last_error="RECOVERY_WORKER_ERROR",
                        next_retry_at=retry_at,
                        expected_revision=int(job.get("revision", 0)),
                    )
                    break
                except Exception as record_error:
                    if state is not None:
                        state.failed(record_error)
                    LOGGER.exception("could not persist recovery failure; retrying")
                    await asyncio.sleep(settings.recovery_poll_interval_seconds)
        if state is not None:
            state.succeeded()


if __name__ == "__main__":
    raise SystemExit(main())
