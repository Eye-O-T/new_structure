from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from .config import EdgeConfig, write_atomic
from .monitoring import CameraInputWatchdog
from .pipeline import (
    build_gstreamer_command,
    daily_backup_directory,
    redacted_command,
    render_edge_mediamtx_config,
)
from .retention import enforce_retention
from .state import (
    EventJournal,
    ProfileRequestStore,
    ProfileSelectionStore,
    RuntimeStatusStore,
    default_runtime_root,
    default_state_root,
    utc_timestamp,
)

try:  # The service targets Linux; this fallback keeps imports portable.
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX test hosts only
    fcntl = None  # type: ignore[assignment]


LOGGER = logging.getLogger("ai_cctv.edge")


class CameraInputLostError(RuntimeError):
    pass


class PipelineStartError(RuntimeError):
    pass


class EdgeRunner:
    def __init__(
        self,
        config_path: str | Path,
        *,
        state_root: Path | None = None,
        runtime_root: Path | None = None,
    ):
        self.config_path = Path(config_path)
        self.base_config = EdgeConfig.load(config_path)
        self.config = self.base_config
        self.profile_generation = 0
        self.runner_instance_id = uuid.uuid4().hex
        self.stop_event = threading.Event()
        self.reload_event = threading.Event()
        self.children: list[subprocess.Popen] = []
        self.capture: subprocess.Popen | None = None
        self.publisher: subprocess.Popen | None = None
        self.publisher_restart_at = 0.0
        self.publisher_started_at = 0.0
        self.publisher_delay = 1.0
        self.central_connection_status = (
            "unknown" if self.config.rtsp.mode == "central_publish" else "unknown"
        )
        self.state_root = state_root or default_state_root()
        self.runtime_root = runtime_root or default_runtime_root()
        self.status_store = RuntimeStatusStore(self.state_root)
        self.selection_store = ProfileSelectionStore(self.state_root)
        self.request_store = ProfileRequestStore(self.runtime_root)
        self.events = EventJournal(self.config.camera_id, self.state_root)
        self.camera_watchdog = CameraInputWatchdog(
            self.config.monitoring.frame_timeout_seconds
        )
        self.active_backup_dir: Path | None = None
        self.active_segment_prefix = ""
        self._last_activity: tuple[str, int, int] | None = None
        self.lock_handle = None

    def _lock(self) -> None:
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        lock_path = self.runtime_root / f"{self.config.camera_id}.lock"
        self.lock_handle = lock_path.open("a+", encoding="utf-8")
        if fcntl is not None:
            try:
                fcntl.flock(self.lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise RuntimeError(
                    f"camera_id {self.config.camera_id} is already running"
                ) from exc
        self.lock_handle.seek(0)
        self.lock_handle.truncate()
        self.lock_handle.write(
            json.dumps(
                {
                    "pid": os.getpid(),
                    "runner_instance_id": self.runner_instance_id,
                }
            )
        )
        self.lock_handle.flush()

    def _load_effective_config(self) -> None:
        base = EdgeConfig.load(self.config_path)
        selected, generation = self.selection_store.read(base.video.profile)
        request = self.request_store.read()
        if request is not None:
            if (
                request["runner_pid"] == os.getpid()
                and request["runner_instance_id"] == self.runner_instance_id
            ):
                if self._profile_request_expired(
                    request,
                    base.control.apply_timeout_seconds,
                ):
                    LOGGER.warning("discarding expired transient profile request")
                    self.request_store.clear(
                        generation=int(request["generation"]),
                        runner_instance_id=self.runner_instance_id,
                    )
                    request = None
                else:
                    selected = str(request["profile"])
                    generation = int(request["generation"])
            else:
                self.request_store.clear(
                    generation=int(request["generation"]),
                    runner_instance_id=str(request["runner_instance_id"]),
                )
        if selected not in base.video.supported_profiles:
            LOGGER.error(
                "ignoring unsupported persisted profile %s; using %s",
                selected,
                base.video.profile,
            )
            selected = base.video.profile
            if request is not None:
                self.request_store.clear(
                    generation=int(request["generation"]),
                    runner_instance_id=str(request["runner_instance_id"]),
                )
        self.base_config = base
        self.config = replace(base, video=base.video.with_profile(selected))
        self.profile_generation = generation
        self.camera_watchdog.timeout_seconds = (
            self.config.monitoring.frame_timeout_seconds
        )

    @staticmethod
    def _profile_request_expired(
        request: dict[str, object],
        timeout_seconds: float,
    ) -> bool:
        return time.monotonic() - float(request["requested_monotonic"]) >= timeout_seconds

    def _expire_active_profile_request(self) -> bool:
        request = self.request_store.read()
        if request is None:
            return False
        if (
            request["runner_pid"] != os.getpid()
            or request["runner_instance_id"] != self.runner_instance_id
            or request["profile"] != self.config.video.profile
            or request["generation"] != self.profile_generation
            or not self._profile_request_expired(
                request,
                self.config.control.apply_timeout_seconds,
            )
        ):
            return False
        self.request_store.clear(
            generation=int(request["generation"]),
            runner_instance_id=self.runner_instance_id,
        )
        self.reload_event.set()
        LOGGER.warning(
            "profile request expired before commit; restoring persisted selection"
        )
        return True

    def _write_status(
        self,
        state: str,
        error_code: str | None = None,
        error: str | None = None,
    ) -> None:
        capture_stopped = state == "stopped"
        payload = {
            "camera_id": self.config.camera_id,
            "runner_pid": os.getpid(),
            "runner_instance_id": self.runner_instance_id,
            "state": state,
            "central_mode": self.config.rtsp.mode,
            "central_connection_status": (
                "unknown" if capture_stopped else self.central_connection_status
            ),
            "camera_input": (
                "offline" if capture_stopped else self.camera_watchdog.status
            ),
            "current_video_profile": self.config.video.profile,
            "profile_generation": self.profile_generation,
            "supported_video_profiles": list(self.config.video.supported_profiles),
            "updated_at": utc_timestamp(),
            "last_error_code": error_code,
            "last_error": error,
        }
        self.status_store.write(payload)

    def _spawn(self, command: list[str]) -> subprocess.Popen:
        return subprocess.Popen(command, start_new_session=True)

    def _start_children(self) -> None:
        self.children = []
        if self.config.rtsp.mode == "central_pull":
            generated = self.state_root / "mediamtx.yml"
            write_atomic(generated, render_edge_mediamtx_config(self.config))
            self.children.append(
                self._spawn([str(self.config.rtsp.mediamtx_binary), str(generated)])
            )
            time.sleep(1)

        timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        self.active_backup_dir = daily_backup_directory(self.config, timestamp[:8])
        self.active_segment_prefix = timestamp
        self._last_activity = None
        self.camera_watchdog.arm()
        pipeline = build_gstreamer_command(self.config, timestamp)
        LOGGER.info("starting pipeline: %s", redacted_command(pipeline))
        self.capture = self._spawn(pipeline)
        self.children.append(self.capture)
        if self.config.rtsp.mode == "central_publish":
            self._try_start_publisher()
        self._write_status("starting")

    def _start_publisher(self) -> None:
        self.publisher = self._spawn(
            [
                sys.executable,
                "-m",
                "ai_cctv_edge.publisher",
                "--config",
                str(self.config_path),
            ]
        )
        self.publisher_started_at = time.monotonic()
        if self.central_connection_status != "offline":
            self.central_connection_status = "connecting"

    def _try_start_publisher(self, now: float | None = None) -> None:
        """Keep capture alive even when spawning the network publisher fails."""

        try:
            self._start_publisher()
        except OSError as exc:
            LOGGER.warning(
                "could not start central publisher; local backup continues: %s",
                type(exc).__name__,
            )
            self.publisher = None
            self._set_central_status("offline")
            current = time.monotonic() if now is None else now
            self.publisher_restart_at = current + self.publisher_delay
            self.publisher_delay = min(self.publisher_delay * 2, 30.0)

    def _publisher_confirmed(self) -> bool:
        if self.publisher is None:
            return False
        try:
            payload = json.loads(
                (self.state_root / "publisher-status.json").read_text(encoding="utf-8")
            )
            return (
                int(payload.get("pid", -1)) == self.publisher.pid
                and payload.get("status") == "online"
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return False

    def _set_central_status(self, status: str) -> None:
        previous = self.central_connection_status
        if previous == status:
            return
        self.central_connection_status = status
        if status == "offline" and previous in {"online", "connecting"}:
            self.events.record(
                "central_connection_lost",
                reason="publisher_exited",
            )
        elif status == "online" and previous == "offline":
            self.events.record("central_connection_restored")
        self._write_status(
            "running" if self.camera_watchdog.status == "online" else "starting"
        )

    def _maintain_publisher(self) -> None:
        """Restart network publishing independently from capture and backup."""

        if self.config.rtsp.mode != "central_publish":
            return
        now = time.monotonic()
        if self.publisher is not None and self.publisher.poll() is None:
            self.publisher_delay = 1.0
            if self._publisher_confirmed():
                self._set_central_status("online")
            return
        if self.publisher is not None:
            LOGGER.warning(
                "central publisher exited with code %s; local backup continues",
                self.publisher.returncode,
            )
            self.publisher = None
            self._set_central_status("offline")
            self.publisher_restart_at = now + self.publisher_delay
            self.publisher_delay = min(self.publisher_delay * 2, 30.0)
        if now >= self.publisher_restart_at:
            self._try_start_publisher(now)

    def _recording_activity(self) -> tuple[str, int, int] | None:
        if self.active_backup_dir is None:
            return None
        newest: tuple[str, int, int] | None = None
        try:
            paths = self.active_backup_dir.glob(f"{self.active_segment_prefix}_*.ts")
            for path in paths:
                try:
                    stat = path.stat()
                except FileNotFoundError:
                    continue
                if stat.st_size <= 0:
                    continue
                candidate = (path.name, stat.st_size, stat.st_mtime_ns)
                if newest is None or candidate[2] > newest[2]:
                    newest = candidate
        except OSError:
            return None
        return newest

    def _monitor_camera_input(self) -> None:
        activity = self._recording_activity()
        if activity is not None and activity != self._last_activity:
            self._last_activity = activity
            event_type = self.camera_watchdog.observe_frame()
            if event_type:
                self.events.record(event_type)
            self._write_status("running")
            return

        event_type = self.camera_watchdog.poll()
        if event_type:
            self.events.record(
                event_type,
                reason="no_frame_timeout",
                timeout_seconds=self.config.monitoring.frame_timeout_seconds,
            )
            self._write_status(
                "error",
                "CAMERA_UNAVAILABLE",
                "no_frame_timeout",
            )
            raise CameraInputLostError("no_frame_timeout")

    def _stop_children(self) -> None:
        all_children = self.children + ([self.publisher] if self.publisher else [])
        for child in all_children:
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 8
        for child in all_children:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                child.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(child.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
        self.children.clear()
        self.capture = None
        self.publisher = None

    def _retention_loop(self) -> None:
        while not self.stop_event.wait(30):
            camera_root = self.config.backup.root / self.config.camera_id
            try:
                deleted = enforce_retention(
                    camera_root,
                    self.config.backup.max_bytes,
                    self.config.backup.max_age_hours,
                    preserve_newest=True,
                )
                if deleted:
                    LOGGER.info("retention removed %d segments", len(deleted))
            except OSError:
                LOGGER.exception("retention failed")

    def request_stop(self, *_: object) -> None:
        self.stop_event.set()
        self.reload_event.set()

    def request_reload(self, *_: object) -> None:
        self.reload_event.set()

    def _unlock(self) -> None:
        if self.lock_handle is None:
            return
        lock_path = self.runtime_root / f"{self.config.camera_id}.lock"
        if fcntl is None:
            self.lock_handle.close()
            self.lock_handle = None
            try:
                owner = json.loads(lock_path.read_text(encoding="utf-8"))
                if (
                    owner.get("pid") == os.getpid()
                    and owner.get("runner_instance_id") == self.runner_instance_id
                ):
                    lock_path.unlink(missing_ok=True)
            except (OSError, json.JSONDecodeError, AttributeError):
                pass
            return
        try:
            try:
                owner = json.loads(lock_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                owner = {}
            if (
                owner.get("pid") == os.getpid()
                and owner.get("runner_instance_id") == self.runner_instance_id
            ):
                lock_path.unlink(missing_ok=True)
            fcntl.flock(self.lock_handle, fcntl.LOCK_UN)
        finally:
            self.lock_handle.close()
            self.lock_handle = None

    def run(self) -> int:
        self._lock()
        self.state_root.mkdir(parents=True, exist_ok=True)
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, self.request_reload)
        retention = threading.Thread(target=self._retention_loop, daemon=True)
        retention.start()
        delay = 1.0
        try:
            while not self.stop_event.is_set():
                error_code: str | None = None
                error_message: str | None = None
                try:
                    self._load_effective_config()
                    self.reload_event.clear()
                    self._start_children()
                    started_day = datetime.now(UTC).date()
                    while not self.stop_event.wait(1):
                        if self._expire_active_profile_request():
                            break
                        if self.reload_event.is_set():
                            LOGGER.info("profile reload requested")
                            break
                        self._maintain_publisher()
                        self._monitor_camera_input()
                        failed = next(
                            (
                                child
                                for child in self.children
                                if child.poll() is not None
                            ),
                            None,
                        )
                        if failed is not None:
                            if failed is self.capture:
                                event_type = self.camera_watchdog.mark_lost()
                                if event_type:
                                    self.events.record(
                                        event_type,
                                        reason="pipeline_exited",
                                        timeout_seconds=(
                                            self.config.monitoring.frame_timeout_seconds
                                        ),
                                    )
                                raise CameraInputLostError(
                                    f"capture exited with code {failed.returncode}"
                                )
                            raise PipelineStartError(
                                f"child exited with code {failed.returncode}"
                            )
                        if datetime.now(UTC).date() != started_day:
                            LOGGER.info("UTC day changed; rotating capture pipeline")
                            break
                    delay = 1.0
                except CameraInputLostError as exc:
                    error_code = "CAMERA_UNAVAILABLE"
                    error_message = str(exc)
                    LOGGER.exception("camera input failed")
                except Exception as exc:
                    error_code = "PIPELINE_START_FAILED"
                    error_message = type(exc).__name__
                    LOGGER.exception("edge pipeline failed")
                finally:
                    self._stop_children()

                if self.stop_event.is_set():
                    break
                if self.reload_event.is_set():
                    self.reload_event.clear()
                    continue
                if error_code:
                    self._write_status("error", error_code, error_message)
                    self.reload_event.wait(delay)
                    self.reload_event.clear()
                    delay = min(delay * 2, 30.0)
            self._write_status("stopped")
            return 0
        finally:
            self.stop_event.set()
            self._stop_children()
            self._unlock()
