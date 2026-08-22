from __future__ import annotations

import hashlib
import hmac
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse

from .config import EdgeConfig

FILENAME = re.compile(r"^(\d{8}T\d{6}(?:\.\d+)?Z)_(\d{6})\.ts$")


def _parse(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("timezone is required")
    return parsed.astimezone(UTC)


def _segment_start(path: Path) -> datetime:
    match = FILENAME.fullmatch(path.name)
    if match:
        return _parse(match.group(1)) + timedelta(seconds=(int(match.group(2)) * 10))
    return datetime.fromtimestamp(path.stat().st_mtime - 10, UTC)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def create_app(config_path: str | Path) -> FastAPI:
    config = EdgeConfig.load(config_path)
    expected_token = config.recovery.token_file.read_text(encoding="utf-8").strip()
    if len(expected_token) < 32:
        raise RuntimeError("recovery token must contain at least 32 characters")
    camera_root = config.backup.root / config.camera_id
    app = FastAPI(title="AI_CCTV Edge Recovery", version="0.3.0")

    def authenticate(authorization: str | None = Header(default=None)) -> None:
        supplied = ""
        if authorization and authorization.startswith("Bearer "):
            supplied = authorization.removeprefix("Bearer ")
        if not hmac.compare_digest(supplied, expected_token):
            raise HTTPException(status_code=401, detail="invalid recovery token")

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
        for path in sorted(camera_root.rglob("*.ts")):
            segment_start = _segment_start(path)
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
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid path") from exc
        if not target.is_file() or target.suffix != ".ts":
            raise HTTPException(status_code=404, detail="segment not found")
        return FileResponse(target, media_type="video/mp2t", filename=target.name)

    return app
