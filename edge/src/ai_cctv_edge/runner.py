from __future__ import annotations

import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from .config import EdgeConfig, write_atomic
from .pipeline import (
    build_gstreamer_command,
    redacted_command,
    render_edge_mediamtx_config,
)
from .retention import enforce_retention

LOGGER = logging.getLogger("ai_cctv.edge")


class EdgeRunner:
    def __init__(self, config_path: str | Path):
        self.config_path = Path(config_path)
        self.config = EdgeConfig.load(config_path)
        self.stop_event = threading.Event()
        self.children: list[subprocess.Popen] = []
        self.publisher: subprocess.Popen | None = None
        self.publisher_restart_at = 0.0
        self.publisher_delay = 1.0
        self.state_root = Path("/var/lib/ai-cctv-edge/state")
        self.runtime_root = Path("/run/ai-cctv-edge")
        self.lock_handle = None

    def _lock(self) -> None:
        self.runtime_root.mkdir(parents=True, exist_ok=True)
        lock_path = self.runtime_root / f"{self.config.camera_id}.lock"
        self.lock_handle = lock_path.open("w", encoding="utf-8")
        try:
            fcntl.flock(self.lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError(
                f"camera_id {self.config.camera_id} is already running"
            ) from exc
        self.lock_handle.write(str(os.getpid()))
        self.lock_handle.flush()

    def _write_status(self, state: str, error: str | None = None) -> None:
        payload = {
            "camera_id": self.config.camera_id,
            "state": state,
            "central_mode": self.config.rtsp.mode,
            "updated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "last_error": error,
        }
        write_atomic(
            self.state_root / "status.json",
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            mode=0o640,
        )

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
        pipeline = build_gstreamer_command(self.config, timestamp)
        LOGGER.info("starting pipeline: %s", redacted_command(pipeline))
        self.children.append(self._spawn(pipeline))
        self.children.append(
            self._spawn(
                [
                    sys.executable,
                    "-m",
                    "ai_cctv_edge.cli",
                    "--config",
                    str(self.config_path),
                    "serve-recovery",
                ]
            )
        )
        if self.config.rtsp.mode == "central_publish":
            self._start_publisher()
        self._write_status("running")

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

    def _maintain_publisher(self) -> None:
        """Restart network publishing independently from capture and backup."""

        if self.config.rtsp.mode != "central_publish":
            return
        if self.publisher is not None and self.publisher.poll() is None:
            self.publisher_delay = 1.0
            return
        now = time.monotonic()
        if self.publisher is not None:
            LOGGER.warning(
                "central publisher exited with code %s; local backup continues",
                self.publisher.returncode,
            )
            self.publisher = None
            self.publisher_restart_at = now + self.publisher_delay
            self.publisher_delay = min(self.publisher_delay * 2, 30.0)
        if now >= self.publisher_restart_at:
            self._start_publisher()

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
        self.publisher = None

    def _retention_loop(self) -> None:
        camera_root = self.config.backup.root / self.config.camera_id
        while not self.stop_event.wait(30):
            try:
                deleted = enforce_retention(
                    camera_root,
                    self.config.backup.max_bytes,
                    self.config.backup.max_age_hours,
                )
                if deleted:
                    LOGGER.info("retention removed %d segments", len(deleted))
            except OSError:
                LOGGER.exception("retention failed")

    def request_stop(self, *_: object) -> None:
        self.stop_event.set()

    def run(self) -> int:
        self._lock()
        self.state_root.mkdir(parents=True, exist_ok=True)
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        retention = threading.Thread(target=self._retention_loop, daemon=True)
        retention.start()
        delay = 1.0
        try:
            while not self.stop_event.is_set():
                try:
                    self._start_children()
                    started_day = datetime.now(UTC).date()
                    while not self.stop_event.wait(1):
                        self._maintain_publisher()
                        failed = next(
                            (
                                child
                                for child in self.children
                                if child.poll() is not None
                            ),
                            None,
                        )
                        if failed is not None:
                            raise RuntimeError(
                                f"child exited with code {failed.returncode}"
                            )
                        if datetime.now(UTC).date() != started_day:
                            LOGGER.info("UTC day changed; rotating capture pipeline")
                            break
                    delay = 1.0
                except Exception as exc:
                    self._write_status("error", type(exc).__name__)
                    LOGGER.exception("edge pipeline failed")
                    if not self.stop_event.wait(delay):
                        delay = min(delay * 2, 30.0)
                finally:
                    self._stop_children()
            self._write_status("stopped")
            return 0
        finally:
            self.stop_event.set()
            self._stop_children()
