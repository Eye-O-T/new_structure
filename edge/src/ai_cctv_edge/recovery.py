# 중앙 서버가 장애 구간의 로컬 녹화를 가져갈 수 있도록 목록과 TS 파일을 제공한다.
# 목록의 SHA-256은 전송 후 파일이 원본과 같은지 확인하는 지문이며 암호화는 아니다.
from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse

from .auth import BearerAuthenticator, load_tokens
from .config import EdgeConfig
from .segments import read_segment_metadata, segment_overlaps

# 복구 요청의 시간대가 있는 ISO 시각을 UTC로 맞추고 시간대 없는 값은 거부한다.
def _parse(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("timezone is required")
    return parsed.astimezone(UTC)


# 캡처 시작 시각과 세그먼트 번호로 시작을 계산하며 구형 파일명은 수정 시각으로 추정한다.
def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _recoverable_segments(
    camera_root: Path,
    *,
    camera_id: str,
) -> tuple[Path, ...]:
    """Return nonempty segments with validated completion metadata only."""

    candidates: list[tuple[datetime, str, Path]] = []
    for path in camera_root.rglob("*.ts") if camera_root.exists() else ():
        try:
            stat = path.stat()
        except OSError:
            continue
        metadata = read_segment_metadata(path, camera_id=camera_id)
        if stat.st_size > 0 and metadata is not None:
            try:
                segment_start = _parse(str(metadata["start_time"]))
            except (KeyError, TypeError, ValueError):
                continue
            candidates.append((segment_start, path.as_posix(), path))
    candidates.sort()
    return tuple(item[2] for item in candidates)


def create_app(config_path: str | Path) -> FastAPI:
    config = EdgeConfig.load(config_path)
    authenticate = BearerAuthenticator(
        load_tokens(config.control.token_file, config.recovery.token_file)
    )
    camera_root = (config.backup.root / config.camera_id).resolve()
    app = FastAPI(title="AI_CCTV Edge Recovery", version="0.3.3")

    @app.get("/health/live")
    def health_live():
        return {
            "status": "alive",
            "device_id": config.device_id,
            "camera_id": config.camera_id,
        }

    # 요청 구간과 겹치는 완료 세그먼트의 상대 경로·크기·해시를 반환한다.
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
            camera_id=config.camera_id,
        ):
            metadata = read_segment_metadata(path, camera_id=config.camera_id)
            if metadata is None:
                continue
            segment_start = _parse(str(metadata["start_time"]))
            segment_end = _parse(str(metadata["end_time"]))
            if segment_overlaps(segment_start, segment_end, query_start, query_end):
                try:
                    stat = path.stat()
                    checksum = _sha256(path)
                except OSError:
                    # Retention may remove a finalized file during this query.
                    continue
                items.append(
                    {
                        "camera_id": config.camera_id,
                        "start_time": segment_start.isoformat().replace("+00:00", "Z"),
                        "end_time": segment_end.isoformat().replace("+00:00", "Z"),
                        "relative_path": path.relative_to(camera_root).as_posix(),
                        "size": stat.st_size,
                        "sha256": checksum,
                    }
                )
        return {"camera_id": config.camera_id, "items": items}

    # 목록 조회 이후에도 파일이 완료된 상태인지 다시 확인한 뒤 TS 영상을 전달한다.
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
            camera_id=config.camera_id,
        ):
            raise HTTPException(status_code=409, detail="segment is not finalized")
        return FileResponse(target, media_type="video/mp2t", filename=target.name)

    return app
