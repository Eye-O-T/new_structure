from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from fastapi import Depends, FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .auth import BearerAuthenticator, load_tokens
from .config import EdgeConfig, VIDEO_PROFILES
from .monitoring import (
    LinuxPowerSupplySensor,
    PowerEventDetector,
    PowerMonitor,
    PowerSensor,
    SystemMetricsCollector,
)
from .pipeline import build_profile_probe_command
from .state import (
    EventJournal,
    ProfileRequestStore,
    ProfileSelectionStore,
    RuntimeStatusStore,
    default_runtime_root,
    default_state_root,
    utc_timestamp,
)

try:  # Verify the runner's held lock on Linux; keep test imports portable.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX test hosts only
    fcntl = None  # type: ignore[assignment]


@dataclass(frozen=True)
class VideoCapabilities:
    supported_profiles: tuple[str, ...]
    camera_available: bool | None
    encoder_available: bool | None

    @property
    def status(self) -> str:
        if self.camera_available is False or self.encoder_available is False:
            return "unavailable"
        if self.camera_available is None or self.encoder_available is None:
            return "unknown"
        return "available"


@dataclass(frozen=True)
class CameraMode:
    width: int
    height: int
    max_fps: float


_CAMERA_HEADER = re.compile(r"^\s*(?P<index>\d+)\s*:")
_CAMERA_MODE = re.compile(
    r"(?P<width>\d+)[x×](?P<height>\d+)\s*"
    r"\[(?P<fps>\d+(?:\.\d+)?)\s+fps\b",
    re.IGNORECASE,
)
_NOMINAL_FPS_TOLERANCE = 0.05


def _parse_primary_camera_modes(output: str) -> tuple[CameraMode, ...]:
    """Parse sensor modes for camera index 0 from rpicam/libcamera output."""

    in_primary_camera = False
    found_primary_camera = False
    modes: list[CameraMode] = []
    for line in output.splitlines():
        header = _CAMERA_HEADER.match(line)
        if header is not None:
            if found_primary_camera:
                break
            in_primary_camera = header.group("index") == "0"
            found_primary_camera = in_primary_camera
            continue
        if not in_primary_camera:
            continue
        for match in _CAMERA_MODE.finditer(line):
            modes.append(
                CameraMode(
                    width=int(match.group("width")),
                    height=int(match.group("height")),
                    max_fps=float(match.group("fps")),
                )
            )
    return tuple(modes)


class CapabilityProbe(Protocol):
    def inspect(self, config: EdgeConfig) -> VideoCapabilities: ...


class LocalCapabilityProbe:
    """Probe the primary camera's sensor modes and the configured encoder."""

    def __init__(self, timeout_seconds: float = 5.0):
        self.timeout_seconds = timeout_seconds
        self._cache: tuple[float, VideoCapabilities] | None = None
        self._lock = threading.Lock()

    def _encoder_available(self, encoder: str) -> bool | None:
        inspect = shutil.which("gst-inspect-1.0")
        if inspect is None:
            return None
        try:
            result = subprocess.run(
                [inspect, encoder],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return result.returncode == 0

    def _camera_modes(self) -> tuple[bool | None, tuple[CameraMode, ...] | None]:
        tool = shutil.which("rpicam-hello") or shutil.which("libcamera-hello")
        if tool is None:
            return None, None
        try:
            result = subprocess.run(
                [tool, "--list-cameras"],
                capture_output=True,
                text=True,
                timeout=self.timeout_seconds,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None, None
        if result.returncode != 0 or "No cameras available" in result.stdout:
            return False, ()
        if "Available cameras" not in result.stdout:
            return None, None
        modes = _parse_primary_camera_modes(result.stdout)
        if not modes:
            # A detected camera without parseable FPS data is not enough to
            # claim that a requested 30fps mode is supported.
            return None, None
        return True, modes

    @staticmethod
    def _supported_profiles(
        config: EdgeConfig,
        modes: tuple[CameraMode, ...] | None,
    ) -> tuple[str, ...]:
        if modes is None:
            # Preserve configured declarations while exposing an unknown
            # capability status; ProfileManager rejects changes as unknown.
            return tuple(config.video.supported_profiles)
        supported: list[str] = []
        for name in config.video.supported_profiles:
            profile = VIDEO_PROFILES[name]
            if any(
                mode.width >= profile.width
                and mode.height >= profile.height
                and mode.max_fps + _NOMINAL_FPS_TOLERANCE >= profile.fps
                for mode in modes
            ):
                supported.append(name)
        return tuple(supported)

    def inspect(self, config: EdgeConfig) -> VideoCapabilities:
        now = time.monotonic()
        with self._lock:
            if self._cache is not None and now - self._cache[0] < 30:
                return self._cache[1]
        camera_available, modes = self._camera_modes()
        result = VideoCapabilities(
            supported_profiles=self._supported_profiles(config, modes),
            camera_available=camera_available,
            encoder_available=self._encoder_available(config.video.encoder),
        )
        with self._lock:
            self._cache = (now, result)
        return result


@dataclass(frozen=True)
class ActivationResult:
    status: str
    reason_code: str | None = None


class ProfileRuntime(Protocol):
    def current_profile(self, default_profile: str) -> str: ...

    def persisted_profile(self, default_profile: str) -> str: ...

    def generation(self, default_profile: str) -> int: ...

    def preflight(self, candidate: EdgeConfig, timeout_seconds: float) -> None: ...

    def activate(self, profile: str, generation: int) -> None: ...

    def commit(self, profile: str, generation: int) -> None: ...

    def clear_request(self, generation: int) -> None: ...

    def wait_for(
        self,
        profile: str,
        generation: int,
        timeout_seconds: float,
    ) -> ActivationResult: ...


class ApplyFailure(RuntimeError):
    def __init__(self, reason_code: str, message: str):
        super().__init__(message)
        self.reason_code = reason_code
        self.message = message


class LocalProfileRuntime:
    def __init__(
        self,
        config: EdgeConfig,
        selection_store: ProfileSelectionStore,
        status_store: RuntimeStatusStore,
        runtime_root: Path | None = None,
    ):
        self.config = config
        self.selection_store = selection_store
        self.status_store = status_store
        self.runtime_root = runtime_root or default_runtime_root()
        self.request_store = ProfileRequestStore(self.runtime_root)

    def current_profile(self, default_profile: str) -> str:
        status = self.status_store.read()
        profile = status.get("current_video_profile")
        if profile in VIDEO_PROFILES:
            return str(profile)
        return self.selection_store.read(default_profile)[0]

    def persisted_profile(self, default_profile: str) -> str:
        return self.selection_store.read(default_profile)[0]

    def generation(self, default_profile: str) -> int:
        selected_generation = self.selection_store.read(default_profile)[1]
        status = self.status_store.read()
        try:
            status_generation = int(status.get("profile_generation", 0))
        except (TypeError, ValueError):
            status_generation = 0
        return max(selected_generation, status_generation)

    def preflight(self, candidate: EdgeConfig, timeout_seconds: float) -> None:
        try:
            result = subprocess.run(
                build_profile_probe_command(candidate),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=timeout_seconds,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise ApplyFailure(
                "CONTROL_TIMEOUT", "video profile preflight timed out"
            ) from exc
        except OSError as exc:
            raise ApplyFailure(
                "ENCODER_UNAVAILABLE", "GStreamer profile preflight is unavailable"
            ) from exc
        if result.returncode != 0:
            raise ApplyFailure(
                "PIPELINE_START_FAILED", "temporary encoder pipeline failed"
            )

    def _runner_identity(self) -> tuple[int, str]:
        lock_path = self.runtime_root / f"{self.config.camera_id}.lock"
        try:
            with lock_path.open("r+", encoding="utf-8") as handle:
                owner = json.loads(handle.read())
                pid = int(owner["pid"])
                runner_instance_id = str(owner["runner_instance_id"])
                if pid <= 0 or len(runner_instance_id) < 16:
                    raise ValueError
                if fcntl is not None:
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    except BlockingIOError:
                        pass
                    else:
                        fcntl.flock(handle, fcntl.LOCK_UN)
                        raise ApplyFailure(
                            "EDGE_OFFLINE", "edge runner lock is not held"
                        )
            os.kill(pid, 0)
            status = self.status_store.read()
            if (
                status.get("runner_pid") != pid
                or status.get("runner_instance_id") != runner_instance_id
                or status.get("state") == "stopped"
            ):
                raise ApplyFailure(
                    "EDGE_OFFLINE", "edge runner status does not match its lock"
                )
        except ApplyFailure:
            raise
        except (
            FileNotFoundError,
            KeyError,
            TypeError,
            ValueError,
            OSError,
            json.JSONDecodeError,
        ) as exc:
            raise ApplyFailure(
                "EDGE_OFFLINE", "edge capture service is offline"
            ) from exc
        return pid, runner_instance_id

    def activate(self, profile: str, generation: int) -> None:
        pid, runner_instance_id = self._runner_identity()
        try:
            self.request_store.write(
                profile,
                generation,
                pid,
                runner_instance_id,
            )
        except OSError as exc:
            raise ApplyFailure(
                "PIPELINE_START_FAILED", "could not stage the profile request"
            ) from exc
        try:
            reload_signal = getattr(signal, "SIGHUP", signal.SIGTERM)
            os.kill(pid, reload_signal)
        except OSError as exc:
            try:
                self.request_store.clear(
                    generation=generation,
                    runner_instance_id=runner_instance_id,
                )
            except OSError:
                pass
            raise ApplyFailure(
                "EDGE_OFFLINE", "edge capture service is offline"
            ) from exc

    def commit(self, profile: str, generation: int) -> None:
        request = self.request_store.read()
        if (
            request is None
            or request["profile"] != profile
            or request["generation"] != generation
        ):
            raise ApplyFailure(
                "PIPELINE_START_FAILED",
                "verified profile request is no longer current",
            )
        try:
            self.selection_store.write(profile, generation)
        except OSError as exc:
            raise ApplyFailure(
                "PIPELINE_START_FAILED", "could not persist the verified profile"
            ) from exc
        try:
            self.request_store.clear(
                generation=generation,
                runner_instance_id=str(request["runner_instance_id"]),
            )
        except OSError:
            # The persistent selection is already committed. A same-profile
            # transient file is harmless and is discarded by the next runner.
            pass

    def clear_request(self, generation: int) -> None:
        request = self.request_store.read()
        if request is not None:
            try:
                self.request_store.clear(
                    generation=generation,
                    runner_instance_id=str(request["runner_instance_id"]),
                )
            except OSError:
                pass

    def wait_for(
        self,
        profile: str,
        generation: int,
        timeout_seconds: float,
    ) -> ActivationResult:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            status = self.status_store.read()
            if status.get("profile_generation") == generation:
                if status.get("state") == "error":
                    return ActivationResult(
                        "failed",
                        str(status.get("last_error_code") or "PIPELINE_START_FAILED"),
                    )
                camera_online = status.get("camera_input") == "online"
                central_online = (
                    self.config.rtsp.mode != "central_publish"
                    or status.get("central_connection_status") == "online"
                )
                if (
                    status.get("state") == "running"
                    and status.get("current_video_profile") == profile
                    and camera_online
                    and central_online
                ):
                    return ActivationResult("applied")
            time.sleep(0.2)
        return ActivationResult("timeout", "CONTROL_TIMEOUT")


class ProfileManager:
    STATUS_BY_REASON = {
        "UNSUPPORTED_VIDEO_PROFILE": 422,
        "CAPABILITY_UNKNOWN": 409,
        "CAMERA_UNAVAILABLE": 409,
        "ENCODER_UNAVAILABLE": 409,
        "PIPELINE_START_FAILED": 409,
        "ROLLBACK_FAILED": 500,
        "EDGE_OFFLINE": 503,
        "CONTROL_TIMEOUT": 504,
    }

    def __init__(
        self,
        config: EdgeConfig,
        capability_probe: CapabilityProbe,
        runtime: ProfileRuntime,
        journal: EventJournal,
    ):
        self.config = config
        self.capability_probe = capability_probe
        self.runtime = runtime
        self.journal = journal
        self._lock = threading.Lock()

    def _rejected(
        self,
        requested: str,
        current: str,
        reason_code: str,
        message: str,
    ) -> tuple[dict[str, object], int]:
        self.journal.record(
            "video_profile_change_failed",
            requested_profile=requested,
            current_profile=current,
            reason_code=reason_code,
        )
        return (
            {
                "status": "rejected",
                "requested_profile": requested,
                "current_profile": current,
                "reason_code": reason_code,
                "message": message,
            },
            self.STATUS_BY_REASON.get(reason_code, 409),
        )

    def apply(self, requested: str) -> tuple[dict[str, object], int]:
        requested = requested.lower()
        timeout = self.config.control.apply_timeout_seconds
        if not self._lock.acquire(timeout=timeout):
            current = self.runtime.current_profile(self.config.video.profile)
            return self._rejected(
                requested,
                current,
                "CONTROL_TIMEOUT",
                "another profile change is still in progress",
            )
        started = time.monotonic()
        try:
            current = self.runtime.current_profile(self.config.video.profile)
            persisted = self.runtime.persisted_profile(self.config.video.profile)
            if requested not in VIDEO_PROFILES or (
                requested not in self.config.video.supported_profiles
            ):
                return self._rejected(
                    requested,
                    current,
                    "UNSUPPORTED_VIDEO_PROFILE",
                    f"video profile {requested!r} is not supported by this edge",
                )
            if requested == current and requested == persisted:
                return (
                    {
                        "status": "applied",
                        "previous_profile": current,
                        "current_profile": current,
                    },
                    200,
                )

            capabilities = self.capability_probe.inspect(self.config)
            if requested not in capabilities.supported_profiles:
                return self._rejected(
                    requested,
                    current,
                    "UNSUPPORTED_VIDEO_PROFILE",
                    f"video profile {requested!r} is not supported by this edge",
                )
            if capabilities.camera_available is False:
                return self._rejected(
                    requested,
                    current,
                    "CAMERA_UNAVAILABLE",
                    "camera input is unavailable",
                )
            if capabilities.encoder_available is False:
                return self._rejected(
                    requested,
                    current,
                    "ENCODER_UNAVAILABLE",
                    f"encoder {self.config.video.encoder} is unavailable",
                )
            if (
                capabilities.camera_available is None
                or capabilities.encoder_available is None
            ):
                return self._rejected(
                    requested,
                    current,
                    "CAPABILITY_UNKNOWN",
                    "camera or encoder capability could not be verified",
                )

            candidate = replace(
                self.config,
                video=self.config.video.with_profile(requested),
            )
            elapsed = time.monotonic() - started
            remaining = max(0.0, timeout - elapsed)
            if remaining <= 0:
                return self._rejected(
                    requested,
                    current,
                    "CONTROL_TIMEOUT",
                    "video profile capability check timed out",
                )
            try:
                self.runtime.preflight(
                    candidate,
                    min(self.config.control.preflight_timeout_seconds, remaining),
                )
            except ApplyFailure as exc:
                return self._rejected(requested, current, exc.reason_code, exc.message)

            generation = self.runtime.generation(self.config.video.profile) + 1
            try:
                self.runtime.activate(requested, generation)
            except ApplyFailure as exc:
                return self._rejected(requested, current, exc.reason_code, exc.message)

            remaining = max(0.0, timeout - (time.monotonic() - started))
            result = self.runtime.wait_for(requested, generation, remaining)
            if result.status == "applied":
                try:
                    self.runtime.commit(requested, generation)
                except ApplyFailure as exc:
                    failure_code = exc.reason_code
                else:
                    self.journal.record(
                        "video_profile_changed",
                        previous_profile=current,
                        current_profile=requested,
                    )
                    return (
                        {
                            "status": "applied",
                            "previous_profile": current,
                            "current_profile": requested,
                        },
                        200,
                    )
            else:
                failure_code = result.reason_code or "PIPELINE_START_FAILED"

            rollback_generation = generation + 1
            try:
                self.runtime.activate(persisted, rollback_generation)
                rollback = self.runtime.wait_for(
                    persisted,
                    rollback_generation,
                    timeout,
                )
            except ApplyFailure:
                rollback = ActivationResult("failed", "ROLLBACK_FAILED")
            if rollback.status != "applied":
                return self._rejected(
                    requested,
                    self.runtime.current_profile(current),
                    "ROLLBACK_FAILED",
                    "new profile failed and the previous profile could not be restored",
                )
            self.runtime.clear_request(rollback_generation)
            return self._rejected(
                requested,
                persisted,
                failure_code,
                "new profile did not become healthy; previous profile was restored",
            )
        finally:
            self._lock.release()


class VideoProfileRequest(BaseModel):
    profile: str


class EdgeStatusService:
    def __init__(
        self,
        config: EdgeConfig,
        selection_store: ProfileSelectionStore,
        status_store: RuntimeStatusStore,
        metrics: SystemMetricsCollector,
        power_monitor: PowerMonitor,
        capability_probe: CapabilityProbe,
    ):
        self.config = config
        self.selection_store = selection_store
        self.status_store = status_store
        self.metrics = metrics
        self.power_monitor = power_monitor
        self.capability_probe = capability_probe

    @staticmethod
    def _capture_process_alive(runtime: dict[str, object]) -> bool | None:
        state = runtime.get("state")
        if state not in {"starting", "running"}:
            return None
        try:
            pid = int(runtime["runner_pid"])
            if pid <= 0:
                return False
            os.kill(pid, 0)
        except PermissionError:
            return True
        except (KeyError, TypeError, ValueError, OSError):
            return False
        return True

    def snapshot(self) -> dict[str, object]:
        runtime = self.status_store.read()
        resources = self.metrics.sample()
        power = self.power_monitor.latest
        if power.power_source == "unknown" and power.battery_percent is None:
            power = self.power_monitor.poll_once()
        desired, _generation = self.selection_store.read(self.config.video.profile)
        current = runtime.get("current_video_profile")
        if current not in VIDEO_PROFILES:
            current = desired
        capabilities = self.capability_probe.inspect(self.config)
        capture_state = runtime.get("state", "unknown")
        capture_alive = self._capture_process_alive(runtime)
        camera_input = runtime.get("camera_input", "unknown")
        if camera_input not in {"online", "offline", "unknown"}:
            camera_input = "unknown"
        central_connection = runtime.get("central_connection_status", "unknown")
        if central_connection not in {"online", "offline", "unknown"}:
            central_connection = "unknown"
        if capture_state in {"stopped", "error"}:
            camera_input = "offline"
            central_connection = "unknown"
        elif capture_alive is False:
            # The control service has an old status file but the capture
            # process no longer exists. Do not report its last healthy values.
            capture_state = "stale"
            camera_input = "offline"
            central_connection = "unknown"
        return {
            "camera_id": self.config.camera_id,
            "online": True,
            "edge_online": True,
            "cpu_percent": resources.cpu_percent,
            "memory_percent": resources.memory_percent,
            "storage_percent": resources.storage_percent,
            "battery_percent": power.battery_percent,
            "power_source": power.power_source,
            "charging": power.charging,
            "camera_input": camera_input,
            "camera_input_status": camera_input,
            "central_connection_status": central_connection,
            "current_video_profile": current,
            "desired_video_profile": desired,
            "supported_video_profiles": list(capabilities.supported_profiles),
            "supported_profiles": list(capabilities.supported_profiles),
            "capability_status": capabilities.status,
            "encoder": self.config.video.encoder,
            "capture_state": capture_state,
            "last_error_code": runtime.get("last_error_code"),
            "last_seen_at": utc_timestamp(),
            "capture_updated_at": runtime.get("updated_at"),
        }


def create_control_app(
    config_path: str | Path,
    *,
    state_root: Path | None = None,
    runtime_root: Path | None = None,
    capability_probe: CapabilityProbe | None = None,
    profile_runtime: ProfileRuntime | None = None,
    power_sensor: PowerSensor | None = None,
    metrics: SystemMetricsCollector | None = None,
) -> FastAPI:
    config = EdgeConfig.load(config_path)
    root = state_root or default_state_root()
    selection_store = ProfileSelectionStore(root)
    status_store = RuntimeStatusStore(root)
    journal = EventJournal(config.camera_id, root)
    probe = capability_probe or LocalCapabilityProbe()
    runtime = profile_runtime or LocalProfileRuntime(
        config,
        selection_store,
        status_store,
        runtime_root,
    )
    monitor = PowerMonitor(
        power_sensor or LinuxPowerSupplySensor(),
        PowerEventDetector(
            config.monitoring.battery_low_percent,
            config.monitoring.battery_critical_percent,
        ),
        journal,
        config.monitoring.interval_seconds,
    )
    status_service = EdgeStatusService(
        config,
        selection_store,
        status_store,
        metrics or SystemMetricsCollector(config.backup.root),
        monitor,
        probe,
    )
    manager = ProfileManager(config, probe, runtime, journal)
    authenticate = BearerAuthenticator(load_tokens(config.control.token_file))

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        monitor.start()
        try:
            yield
        finally:
            monitor.stop()

    app = FastAPI(
        title="AI_CCTV Edge Management",
        version="0.3.0",
        lifespan=lifespan,
    )

    @app.get("/health/live")
    def health_live():
        return {"status": "alive", "camera_id": config.camera_id}

    @app.get("/internal/v1/status", dependencies=[Depends(authenticate)])
    def status():
        return status_service.snapshot()

    @app.get(
        "/internal/v1/capabilities/video",
        dependencies=[Depends(authenticate)],
    )
    def video_capabilities():
        capabilities = probe.inspect(config)
        current = runtime.current_profile(config.video.profile)
        return {
            "camera_id": config.camera_id,
            "supported_profiles": list(capabilities.supported_profiles),
            "current_profile": current,
            "encoder": config.video.encoder,
            "codec": "H.264",
            "capability_status": capabilities.status,
            "camera_available": capabilities.camera_available,
            "encoder_available": capabilities.encoder_available,
        }

    @app.put(
        "/internal/v1/config/video-profile",
        dependencies=[Depends(authenticate)],
    )
    def apply_video_profile(request: VideoProfileRequest):
        payload, status_code = manager.apply(request.profile)
        return JSONResponse(payload, status_code=status_code)

    @app.get("/internal/v1/events", dependencies=[Depends(authenticate)])
    def events(
        after: str | None = None,
        limit: int = Query(default=100, ge=1, le=1000),
    ):
        items, next_cursor, cursor_expired = journal.page(
            after=after,
            limit=limit,
        )
        return {
            "camera_id": config.camera_id,
            "items": items,
            "next_cursor": next_cursor,
            "cursor_expired": cursor_expired,
        }

    app.state.profile_manager = manager
    app.state.status_service = status_service
    app.state.event_journal = journal
    return app
