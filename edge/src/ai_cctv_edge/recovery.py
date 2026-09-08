# 중앙 서버가 장애 구간의 로컬 녹화를 가져갈 수 있도록 목록과 TS 파일을 제공한다.
# 목록의 SHA-256은 전송 후 파일이 원본과 같은지 확인하는 지문이며 암호화는 아니다.
from __future__ import annotations

import hashlib
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse

from .auth import BearerAuthenticator, load_tokens
from .config import EdgeConfig
from .state import default_state_root

FILENAME = re.compile(r"^(\d{8}T\d{6}(?:\.\d+)?Z)_(\d{6})\.ts$")


def _parse(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("timezone is required")
    return parsed.astimezone(UTC)


def _segment_start(path: Path, segment_seconds: int) -> datetime:
    match = FILENAME.fullmatch(path.name)
    if match:
        return _parse(match.group(1)) + timedelta(
            seconds=(int(match.group(2)) * segment_seconds)
        )
    return datetime.fromtimestamp(path.stat().st_mtime - segment_seconds, UTC)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _capture_may_write(camera_id: str) -> bool:
    """마지막 파일이 기록 중일 가능성이 있으면 보수적으로 보호한다."""

    try:
        payload = json.loads(
            (default_state_root() / "status.json").read_text(encoding="utf-8")
        )
        if payload.get("camera_id") != camera_id:
            return True
        state = payload.get("state")
        if state in {"stopped", "error"}:
            return False
        if state not in {"starting", "running"}:
            return True
        pid = int(payload["runner_pid"])
        if pid <= 0:
            return False
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        # 캡처 실행 프로세스가 사라졌다면 마지막 파일도 더 이상 기록되지 않아 복구할 수 있다.
        return False
    except PermissionError:
        return True
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        # 상태를 확인할 수 없으면 기록 중일 수 있다고 보고 마지막 파일의 공개를 보류한다.
        return True


def _recoverable_segments(
    camera_root: Path,
    *,
    capture_may_write: bool,
) -> tuple[Path, ...]:
    """기록이 끝난 영상 조각만 반환한다."""

    candidates: list[tuple[int, str, Path]] = []
    for path in camera_root.rglob("*.ts") if camera_root.exists() else ():
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_size > 0:
            candidates.append((stat.st_mtime_ns, path.as_posix(), path))
    if not candidates:
        return ()
    candidates.sort()
    # 크기·해시 불일치를 막기 위해 캡처 종료가 확인된 경우에만 마지막 파일을 공개한다.
    selected = candidates[:-1] if capture_may_write else candidates
    return tuple(item[2] for item in selected)


def create_app(config_path: str | Path) -> FastAPI:
    config = EdgeConfig.load(config_path)
    authenticate = BearerAuthenticator(
        load_tokens(config.control.token_file, config.recovery.token_file)
    )
    camera_root = (config.backup.root / config.camera_id).resolve()
    app = FastAPI(title="AI_CCTV Edge Recovery", version="0.3.0")

    @app.get("/health/live")
    def health_live():
        return {"status": "alive", "camera_id": config.camera_id}

    @app.get("/v1/recovery/manifest", dependencies=[Depends(authenticate)])
    def manifest(start: str, end: str):
        try:
            query_start, query_end = _parse(start), _parse(end)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if query_start >= query_end:
            raise HTTPException(status_code=422, detail="start must be before end")
        if query_end - query_start > timedelta(hours=24):
            raise HTTPException(status_code=422, detail="range cannot exceed 24 hours")

        items = []
        for path in _recoverable_segments(
            camera_root,
            capture_may_write=_capture_may_write(config.camera_id),
        ):
            segment_start = _segment_start(path, config.backup.segment_seconds)
            segment_end = segment_start + timedelta(
                seconds=config.backup.segment_seconds
            )
            if segment_start < query_end and segment_end > query_start:
                stat = path.stat()
                items.append(
                    {
                        "camera_id": config.camera_id,
                        "start_time": segment_start.isoformat().replace("+00:00", "Z"),
                        "end_time": segment_end.isoformat().replace("+00:00", "Z"),
                        "relative_path": path.relative_to(camera_root).as_posix(),
                        "size": stat.st_size,
                        "sha256": _sha256(path),
                    }
                )
        return {"camera_id": config.camera_id, "items": items}

    @app.get(
        "/v1/recovery/files/{relative_path:path}",
        dependencies=[Depends(authenticate)],
    )
    def recovery_file(relative_path: str):
        root = camera_root.resolve()
        target = (root / relative_path).resolve()
        # ../ 또는 심볼릭 링크로 녹화 폴더 밖 파일을 요청할 수 없도록 최종 경로를 검사한다.
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid path") from exc
        if not target.is_file() or target.suffix != ".ts":
            raise HTTPException(status_code=404, detail="segment not found")
        if target not in _recoverable_segments(
            camera_root,
            capture_may_write=_capture_may_write(config.camera_id),
        ):
            raise HTTPException(status_code=409, detail="segment is not finalized")
        return FileResponse(target, media_type="video/mp2t", filename=target.name)

    return app
