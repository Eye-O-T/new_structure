from __future__ import annotations

import hashlib
import json
import re
import shutil
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .protocol import CAMERA_ID, EventJournal, bearer_matches, utc_timestamp, write_atomic
from .runtime import CentralTarget, MediaEngine, VIDEO_PROFILES


FILENAME = re.compile(r"^(\d{8}T\d{6}(?:\.\d+)?Z)_(\d{6})\.ts$")
SIMULATION_ACTIONS = {
    "central_connection_lost",
    "central_connection_restored",
    "camera_input_lost",
    "camera_input_restored",
    "battery_low",
    "battery_critical",
    "external_power_lost",
    "external_power_restored",
    "storage_warning",
    "storage_critical",
}


class PairingCompletion(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    device_id: str = Field(min_length=1, max_length=128)
    camera_id: str = Field(pattern=CAMERA_ID.pattern)
    central_host: str = Field(min_length=1, max_length=255)
    central_port: int = Field(ge=1, le=65_535)
    backup_root: str = Field(min_length=1, max_length=4096)
    video_profile: Literal["hd", "fhd"] = "hd"
    supported_profiles: list[Literal["hd", "fhd"]] = Field(min_length=1)
    publish_username: str = Field(min_length=1, max_length=256)
    publish_password: str = Field(min_length=16, max_length=1024)


class VideoProfileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: str


class SimulationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: str
    battery_percent: float | None = Field(default=None, ge=0, le=100)
    storage_percent: float | None = Field(default=None, ge=0, le=100)


def _validate_central_host(host: str) -> None:
    if (
        host in {"0.0.0.0", "::"}
        or "://" in host
        or any(character.isspace() or character in "/@?#" for character in host)
    ):
        raise HTTPException(status_code=422, detail="invalid central RTSP host")


def _parse_timestamp(value: str) -> datetime:
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("timezone is required")
    return parsed.astimezone(UTC)


def _segment_start(path: Path, segment_seconds: int) -> datetime:
    match = FILENAME.fullmatch(path.name)
    if match:
        return _parse_timestamp(match.group(1)) + timedelta(
            seconds=int(match.group(2)) * segment_seconds
        )
    return datetime.fromtimestamp(path.stat().st_mtime - segment_seconds, UTC)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class MockEdgeService:
    """State and behavior shared by the control and recovery HTTP apps."""

    def __init__(
        self,
        *,
        device_id: str,
        camera_id: str,
        pairing_key: str,
        state_root: Path,
        backup_root: Path,
        video_path: Path,
        ffmpeg: str = "ffmpeg",
        segment_seconds: int = 10,
        media: MediaEngine | None = None,
    ) -> None:
        if not device_id or len(device_id) > 128:
            raise ValueError("device_id must contain 1..128 characters")
        if CAMERA_ID.fullmatch(camera_id) is None:
            raise ValueError("camera_id is invalid")
        if len(pairing_key) < 32:
            raise ValueError("pairing key must contain at least 32 characters")
        if segment_seconds < 1 or segment_seconds > 3600:
            raise ValueError("segment_seconds must be in range 1..3600")
        self.device_id = device_id
        self.camera_id = camera_id
        self.pairing_key = pairing_key
        self.state_root = state_root.resolve()
        self.backup_root = backup_root.resolve()
        self.segment_seconds = segment_seconds
        self.config_path = self.state_root / "mock-edge.json"
        self.password_path = self.state_root / "publish.password"
        self.marker_path = self.state_root / ".configured"
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.backup_root.mkdir(parents=True, exist_ok=True)
        self.journal = EventJournal(camera_id, self.state_root)
        self._lock = threading.RLock()
        self._camera_input = "online"
        self._battery_percent: float | None = None
        self._power_source = "external"
        self._charging: bool | None = None
        self._storage_override: float | None = None
        self.current_profile = "hd"
        self.supported_profiles = ("hd", "fhd")
        self.media = media or MediaEngine(
            video_path=video_path,
            backup_root=self.backup_root,
            camera_id=camera_id,
            ffmpeg=ffmpeg,
            segment_seconds=segment_seconds,
            event_callback=self._media_event,
        )
        self._load_configuration()

    def _media_event(self, event_type: str, details: dict[str, object]) -> None:
        self.journal.record(event_type, **details)

    @property
    def configured(self) -> bool:
        return self.marker_path.exists() and self.media.configured

    def _load_configuration(self) -> None:
        if not self.marker_path.exists():
            return
        try:
            payload = json.loads(self.config_path.read_text(encoding="utf-8"))
            password = self.password_path.read_text(encoding="utf-8").rstrip("\r\n")
            if payload["device_id"] != self.device_id:
                raise ValueError("persisted device_id does not match CLI device_id")
            if payload["camera_id"] != self.camera_id:
                raise ValueError("persisted camera_id does not match CLI camera_id")
            profiles = tuple(payload["supported_profiles"])
            profile = payload["video_profile"]
            if not profiles or any(item not in VIDEO_PROFILES for item in profiles):
                raise ValueError("persisted supported profiles are invalid")
            if profile not in profiles:
                raise ValueError("persisted selected profile is invalid")
            target = CentralTarget(
                host=payload["central_host"],
                port=payload["central_port"],
                camera_id=self.camera_id,
                username=payload["publish_username"],
                password=password,
            )
            target.validate()
        except (OSError, KeyError, TypeError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError(f"stored Mock Edge configuration is invalid: {exc}") from exc
        self.current_profile = profile
        self.supported_profiles = profiles
        self.media.configure(target, profile)

    def start(self) -> None:
        self.media.start()

    def stop(self) -> None:
        self.media.stop()

    def complete_pairing(self, request: PairingCompletion) -> dict[str, str]:
        with self._lock:
            if self.marker_path.exists():
                raise HTTPException(status_code=409, detail="Edge is already configured")
            if request.device_id != self.device_id or request.camera_id != self.camera_id:
                raise HTTPException(status_code=409, detail="pairing identity mismatch")
            if request.publish_username != request.camera_id:
                raise HTTPException(
                    status_code=422, detail="publish username must match camera_id"
                )
            _validate_central_host(request.central_host)
            if request.publish_password != request.publish_password.strip() or any(
                ord(character) < 0x20 or ord(character) == 0x7F
                for character in request.publish_password
            ):
                raise HTTPException(status_code=422, detail="invalid publish password")
            if not request.backup_root.startswith("/") or "\x00" in request.backup_root:
                raise HTTPException(status_code=422, detail="backup root must be absolute")
            profiles = tuple(dict.fromkeys(request.supported_profiles))
            if request.video_profile not in profiles:
                raise HTTPException(
                    status_code=422, detail="selected profile is not supported"
                )
            target = CentralTarget(
                host=request.central_host,
                port=request.central_port,
                camera_id=self.camera_id,
                username=request.publish_username,
                password=request.publish_password,
            )
            try:
                target.validate()
            except ValueError as exc:
                raise HTTPException(status_code=422, detail=str(exc)) from exc
            payload = {
                "schema_version": 1,
                "device_id": self.device_id,
                "camera_id": self.camera_id,
                "central_host": request.central_host,
                "central_port": request.central_port,
                "video_profile": request.video_profile,
                "supported_profiles": list(profiles),
                "publish_username": request.publish_username,
                "requested_edge_backup_root": request.backup_root,
                "local_backup_root": str(self.backup_root),
            }
            write_atomic(
                self.password_path, request.publish_password + "\n", mode=0o600
            )
            write_atomic(
                self.config_path,
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                mode=0o600,
            )
            try:
                self.media.configure(target, request.video_profile)
                self.current_profile = request.video_profile
                self.supported_profiles = profiles
                write_atomic(self.marker_path, "configured\n", mode=0o644)
            except Exception:
                self.config_path.unlink(missing_ok=True)
                self.password_path.unlink(missing_ok=True)
                raise
            self.journal.record(
                "edge_configured",
                device_id=self.device_id,
                video_profile=self.current_profile,
            )
        return {
            "status": "configured",
            "device_id": self.device_id,
            "camera_id": self.camera_id,
            "rtsp_mode": "central_publish",
        }

    def configure_direct(
        self,
        *,
        central_host: str,
        central_port: int,
        username: str,
        password: str,
        profile: str,
    ) -> None:
        request = PairingCompletion(
            device_id=self.device_id,
            camera_id=self.camera_id,
            central_host=central_host,
            central_port=central_port,
            backup_root="/mock-edge/recordings",
            video_profile=profile,
            supported_profiles=["hd", "fhd"],
            publish_username=username,
            publish_password=password,
        )
        self.complete_pairing(request)

    def status(self) -> dict[str, object]:
        try:
            storage = self._storage_override
            if storage is None:
                usage = shutil.disk_usage(self.backup_root)
                storage = 0.0 if usage.total == 0 else usage.used * 100 / usage.total
        except OSError:
            storage = None
        central = "online" if self.media.publisher_running else "offline"
        capture_state = "running" if self.media.recorder_running else "stopped"
        return {
            "camera_id": self.camera_id,
            "online": True,
            "edge_online": True,
            "cpu_percent": None,
            "memory_percent": None,
            "storage_percent": None if storage is None else round(storage, 2),
            "battery_percent": self._battery_percent,
            "power_source": self._power_source,
            "charging": self._charging,
            "camera_input": self._camera_input,
            "camera_input_status": self._camera_input,
            "central_connection_status": central,
            "current_video_profile": self.current_profile,
            "desired_video_profile": self.current_profile,
            "supported_video_profiles": list(self.supported_profiles),
            "supported_profiles": list(self.supported_profiles),
            "capability_status": "available",
            "encoder": "libx264",
            "capture_state": capture_state,
            "last_error_code": self.media.last_error,
            "last_seen_at": utc_timestamp(),
            "capture_updated_at": utc_timestamp(),
        }

    def apply_profile(self, requested: str) -> tuple[dict[str, object], int]:
        previous = self.current_profile
        if requested not in self.supported_profiles:
            self.journal.record(
                "video_profile_change_failed",
                previous_profile=previous,
                requested_profile=requested,
                current_profile=previous,
                reason_code="UNSUPPORTED_VIDEO_PROFILE",
            )
            return (
                {
                    "status": "rejected",
                    "requested_profile": requested,
                    "current_profile": previous,
                    "reason_code": "UNSUPPORTED_VIDEO_PROFILE",
                    "message": "requested video profile is not supported",
                },
                422,
            )
        applied, reason = self.media.apply_profile(requested)
        if not applied:
            failure_code = reason or "PIPELINE_START_FAILED"
            self.journal.record(
                "video_profile_change_failed",
                previous_profile=previous,
                requested_profile=requested,
                current_profile=previous,
                reason_code=failure_code,
            )
            return (
                {
                    "status": "rejected",
                    "requested_profile": requested,
                    "current_profile": previous,
                    "reason_code": failure_code,
                    "message": "video pipeline could not apply the requested profile",
                },
                503,
            )
        self.current_profile = requested
        self._persist_profile(requested)
        self.journal.record(
            "video_profile_changed",
            previous_profile=previous,
            current_profile=requested,
        )
        return (
            {
                "status": "applied",
                "previous_profile": previous,
                "current_profile": requested,
            },
            200,
        )

    def _persist_profile(self, profile: str) -> None:
        try:
            payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        payload["video_profile"] = profile
        write_atomic(
            self.config_path,
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            mode=0o600,
        )

    def simulate(self, request: SimulationRequest) -> dict[str, object]:
        action = request.action
        if action not in SIMULATION_ACTIONS:
            raise HTTPException(status_code=422, detail="unsupported simulation action")
        if action == "central_connection_lost":
            self.media.suspend_publisher()
        elif action == "central_connection_restored":
            self.media.resume_publisher()
        elif action == "camera_input_lost":
            self._camera_input = "offline"
            self.journal.record(
                action,
                reason="mock_operator_request",
                timeout_seconds=0,
            )
        elif action == "camera_input_restored":
            self._camera_input = "online"
            self.journal.record(action, reason="mock_operator_request")
        elif action in {"battery_low", "battery_critical"}:
            default = 20.0 if action == "battery_low" else 5.0
            self._battery_percent = (
                request.battery_percent
                if request.battery_percent is not None
                else default
            )
            self._power_source = "battery"
            self._charging = False
            self.journal.record(
                action,
                battery_percent=self._battery_percent,
                power_source=self._power_source,
            )
        elif action == "external_power_lost":
            if request.battery_percent is not None:
                self._battery_percent = request.battery_percent
            self._power_source = "battery"
            self._charging = False
            self.journal.record(
                action,
                battery_percent=self._battery_percent,
                power_source=self._power_source,
            )
        elif action == "external_power_restored":
            self._power_source = "external"
            self._charging = self._battery_percent is not None
            self.journal.record(
                action,
                battery_percent=self._battery_percent,
                power_source=self._power_source,
            )
        elif action in {"storage_warning", "storage_critical"}:
            default = 90.0 if action == "storage_warning" else 97.0
            self._storage_override = (
                request.storage_percent
                if request.storage_percent is not None
                else default
            )
            self.journal.record(action, storage_percent=self._storage_override)
        return {"status": "applied", "action": action, "edge": self.status()}

    def finalized_segments(self) -> tuple[Path, ...]:
        camera_root = self.backup_root / self.camera_id
        candidates: list[tuple[int, str, Path]] = []
        for path in camera_root.rglob("*.ts") if camera_root.exists() else ():
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_size > 0:
                candidates.append((stat.st_mtime_ns, path.as_posix(), path))
        candidates.sort()
        if self.media.recorder_running and candidates:
            candidates = candidates[:-1]
        return tuple(item[2] for item in candidates)


def _auth_dependency(service: MockEdgeService):
    def authenticate(authorization: str | None = Header(default=None)) -> None:
        if not bearer_matches(authorization, service.pairing_key):
            raise HTTPException(status_code=401, detail="invalid bearer token")

    return authenticate


def create_control_app(service: MockEdgeService) -> FastAPI:
    authenticate = _auth_dependency(service)
    app = FastAPI(title="AI_CCTV Mock Edge Management", version="0.3.0")

    @app.get("/health/live")
    def health_live():
        return {
            "status": "alive" if service.configured else "pairing",
            "device_id": service.device_id,
            "camera_id": service.camera_id,
        }

    @app.put(
        "/internal/v1/pairing/complete", dependencies=[Depends(authenticate)]
    )
    def complete_pairing(request: PairingCompletion):
        return service.complete_pairing(request)

    @app.get("/internal/v1/status", dependencies=[Depends(authenticate)])
    def status():
        return service.status()

    @app.get(
        "/internal/v1/capabilities/video", dependencies=[Depends(authenticate)]
    )
    def capabilities():
        return {
            "camera_id": service.camera_id,
            "supported_profiles": list(service.supported_profiles),
            "current_profile": service.current_profile,
            "encoder": "libx264",
            "codec": "H.264",
            "capability_status": "available",
            "camera_available": True,
            "encoder_available": True,
        }

    @app.put(
        "/internal/v1/config/video-profile", dependencies=[Depends(authenticate)]
    )
    def apply_video_profile(request: VideoProfileRequest):
        payload, status_code = service.apply_profile(request.profile)
        return JSONResponse(payload, status_code=status_code)

    @app.get("/internal/v1/events", dependencies=[Depends(authenticate)])
    def events(
        after: str | None = None,
        limit: int = Query(default=100, ge=1, le=1000),
    ):
        items, next_cursor, cursor_expired = service.journal.page(
            after=after, limit=limit
        )
        return {
            "camera_id": service.camera_id,
            "items": items,
            "next_cursor": next_cursor,
            "cursor_expired": cursor_expired,
        }

    @app.post("/mock/v1/simulate", dependencies=[Depends(authenticate)])
    def simulate(request: SimulationRequest):
        return service.simulate(request)

    app.state.mock_edge = service
    return app


def create_recovery_app(service: MockEdgeService) -> FastAPI:
    authenticate = _auth_dependency(service)
    camera_root = (service.backup_root / service.camera_id).resolve()
    app = FastAPI(title="AI_CCTV Mock Edge Recovery", version="0.3.0")

    @app.get("/health/live")
    def health_live():
        return {"status": "alive", "camera_id": service.camera_id}

    @app.get("/v1/recovery/manifest", dependencies=[Depends(authenticate)])
    def manifest(start: str, end: str):
        try:
            query_start, query_end = _parse_timestamp(start), _parse_timestamp(end)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if query_start >= query_end:
            raise HTTPException(status_code=422, detail="start must be before end")
        if query_end - query_start > timedelta(hours=24):
            raise HTTPException(status_code=422, detail="range cannot exceed 24 hours")
        items = []
        for path in service.finalized_segments():
            segment_start = _segment_start(path, service.segment_seconds)
            segment_end = segment_start + timedelta(seconds=service.segment_seconds)
            if segment_start < query_end and segment_end > query_start:
                stat = path.stat()
                items.append(
                    {
                        "camera_id": service.camera_id,
                        "start_time": segment_start.isoformat().replace("+00:00", "Z"),
                        "end_time": segment_end.isoformat().replace("+00:00", "Z"),
                        "relative_path": path.relative_to(camera_root).as_posix(),
                        "size": stat.st_size,
                        "sha256": _sha256(path),
                    }
                )
        return {"camera_id": service.camera_id, "items": items}

    @app.get(
        "/v1/recovery/files/{relative_path:path}",
        dependencies=[Depends(authenticate)],
    )
    def recovery_file(relative_path: str):
        target = (camera_root / relative_path).resolve()
        try:
            target.relative_to(camera_root)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid path") from exc
        if not target.is_file() or target.suffix != ".ts":
            raise HTTPException(status_code=404, detail="segment not found")
        if target not in service.finalized_segments():
            raise HTTPException(status_code=409, detail="segment is not finalized")
        return FileResponse(target, media_type="video/mp2t", filename=target.name)

    app.state.mock_edge = service
    return app
