from __future__ import annotations

import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .state import EventJournal


@dataclass(frozen=True)
class PowerReading:
    battery_percent: int | None = None
    power_source: str = "unknown"
    charging: bool | None = None


class PowerSensor(Protocol):
    def read(self) -> PowerReading: ...


class LinuxPowerSupplySensor:
    """Use the documented Linux power-supply ABI; make no UPS register guesses."""

    def __init__(self, root: Path = Path("/sys/class/power_supply")):
        self.root = root

    @staticmethod
    def _text(path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8").strip()
        except OSError:
            return None

    def read(self) -> PowerReading:
        try:
            supplies = [item for item in self.root.iterdir() if item.is_dir()]
        except OSError:
            return PowerReading()

        battery_percent: int | None = None
        charging: bool | None = None
        external_online: list[bool] = []
        battery_seen = False
        for supply in supplies:
            supply_type = self._text(supply / "type")
            if supply_type == "Battery":
                battery_seen = True
                capacity = self._text(supply / "capacity")
                try:
                    candidate = int(capacity) if capacity is not None else None
                except ValueError:
                    candidate = None
                if candidate is not None and 0 <= candidate <= 100:
                    battery_percent = candidate
                status = self._text(supply / "status")
                if status is not None:
                    charging = status.lower() == "charging"
            elif supply_type and (
                supply_type in {"Mains", "UPS", "Wireless"}
                or supply_type.startswith("USB")
            ):
                online = self._text(supply / "online")
                if online in {"0", "1"}:
                    external_online.append(online == "1")

        if any(external_online):
            source = "external"
        elif external_online and battery_seen:
            source = "battery"
        else:
            source = "unknown"
        return PowerReading(battery_percent, source, charging)


@dataclass(frozen=True)
class ResourceSnapshot:
    cpu_percent: float | None
    memory_percent: float | None
    storage_percent: float | None


class SystemMetricsCollector:
    def __init__(
        self,
        storage_path: Path,
        proc_root: Path = Path("/proc"),
    ):
        self.storage_path = storage_path
        self.proc_root = proc_root
        self._previous_cpu: tuple[int, int] | None = None
        self._lock = threading.Lock()

    def _cpu(self) -> float | None:
        try:
            fields = (
                (self.proc_root / "stat")
                .read_text(encoding="utf-8")
                .splitlines()[0]
                .split()
            )
            if fields[0] != "cpu":
                return None
            values = [int(item) for item in fields[1:]]
            total = sum(values)
            idle = values[3] + (values[4] if len(values) > 4 else 0)
        except (OSError, ValueError, IndexError):
            return None

        with self._lock:
            previous = self._previous_cpu
            self._previous_cpu = (total, idle)
        if previous is None:
            delta_total, delta_idle = total, idle
        else:
            delta_total = total - previous[0]
            delta_idle = idle - previous[1]
        if delta_total <= 0:
            return None
        return round(max(0.0, min(100.0, 100 * (1 - delta_idle / delta_total))), 1)

    def _memory(self) -> float | None:
        try:
            values = {}
            for line in (
                (self.proc_root / "meminfo").read_text(encoding="utf-8").splitlines()
            ):
                name, value = line.split(":", maxsplit=1)
                values[name] = int(value.strip().split()[0])
            total = values["MemTotal"]
            available = values.get("MemAvailable", values.get("MemFree", 0))
        except (OSError, ValueError, KeyError, IndexError):
            return None
        if total <= 0:
            return None
        return round(max(0.0, min(100.0, 100 * (total - available) / total)), 1)

    def _storage(self) -> float | None:
        target = self.storage_path
        while not target.exists() and target != target.parent:
            target = target.parent
        try:
            usage = shutil.disk_usage(target)
        except OSError:
            return None
        if usage.total <= 0:
            return None
        return round(100 * (usage.total - usage.free) / usage.total, 1)

    def sample(self) -> ResourceSnapshot:
        return ResourceSnapshot(self._cpu(), self._memory(), self._storage())


class PowerEventDetector:
    def __init__(self, low_percent: int, critical_percent: int):
        self.low_percent = low_percent
        self.critical_percent = critical_percent
        self._power_source: str | None = None
        self._battery_level = "normal"

    def consume(self, reading: PowerReading) -> list[str]:
        events: list[str] = []
        previous_source = self._power_source
        if reading.power_source in {"external", "battery"}:
            self._power_source = reading.power_source
            if previous_source == "external" and reading.power_source == "battery":
                events.append("external_power_lost")
            elif previous_source == "battery" and reading.power_source == "external":
                events.append("external_power_restored")

        percent = reading.battery_percent
        if percent is None:
            return events
        if percent <= self.critical_percent:
            level = "critical"
        elif percent <= self.low_percent:
            level = "low"
        else:
            level = "normal"
        if level != self._battery_level:
            if level == "critical":
                events.append("battery_critical")
            elif level == "low":
                events.append("battery_low")
            self._battery_level = level
        return events


class PowerMonitor:
    def __init__(
        self,
        sensor: PowerSensor,
        detector: PowerEventDetector,
        journal: EventJournal,
        interval_seconds: float,
    ):
        self.sensor = sensor
        self.detector = detector
        self.journal = journal
        self.interval_seconds = interval_seconds
        self._latest = PowerReading()
        self._lock = threading.Lock()
        self._poll_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def latest(self) -> PowerReading:
        with self._lock:
            return self._latest

    def poll_once(self) -> PowerReading:
        with self._poll_lock:
            reading = self.sensor.read()
            with self._lock:
                self._latest = reading
            for event_type in self.detector.consume(reading):
                self.journal.record(
                    event_type,
                    battery_percent=reading.battery_percent,
                    power_source=reading.power_source,
                )
            return reading

    def _run(self) -> None:
        while not self._stop.is_set():
            self.poll_once()
            self._stop.wait(self.interval_seconds)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=max(1.0, self.interval_seconds + 0.5))


class CameraInputWatchdog:
    """State machine fed by observed recording activity from the capture path."""

    def __init__(
        self,
        timeout_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.timeout_seconds = timeout_seconds
        self.clock = clock
        self.last_frame_at = clock()
        self.status = "starting"

    def arm(self) -> None:
        self.last_frame_at = self.clock()
        if self.status != "offline":
            self.status = "starting"

    def observe_frame(self) -> str | None:
        previous = self.status
        self.last_frame_at = self.clock()
        self.status = "online"
        return "camera_input_restored" if previous == "offline" else None

    def poll(self) -> str | None:
        if self.status != "offline" and (
            self.clock() - self.last_frame_at >= self.timeout_seconds
        ):
            return self.mark_lost()
        return None

    def mark_lost(self) -> str | None:
        if self.status == "offline":
            return None
        self.status = "offline"
        return "camera_input_lost"
