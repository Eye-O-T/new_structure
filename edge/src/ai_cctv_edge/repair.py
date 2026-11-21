"""Persistent local snapshots and restart recovery for Edge Repair."""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import EdgeConfig
from .network_policy import apply_persistent
from .network import selected_interface_ipv4
from .state import ProfileSelectionStore, default_state_root
from .startup import invalidate_startup

REPAIR_ROOT = Path("/var/lib/ai-cctv-edge/repair")
PACKAGE_TRANSITION = REPAIR_ROOT / "package-transition"
UNITS = (
    "ai-cctv-edge-network.service",
    "ai-cctv-edge.service",
    "ai-cctv-edge-control.service",
    "ai-cctv-edge-recovery.service",
)


class RepairLockBusy(RuntimeError):
    """Another process currently owns the Repair/configuration lock."""


def _sync_dir(path: Path) -> None:
    if os.name != "posix":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_journal(path: Path, data: dict) -> None:
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.chmod(temp, 0o600)
    os.replace(temp, path)
    _sync_dir(path.parent)


def _systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["systemctl", *args], capture_output=True, text=True, timeout=30)
    if check and result.returncode:
        raise RuntimeError(f"systemctl {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def _repair_owner_is_running(journal: dict) -> bool:
    owner = journal.get("owner_pid")
    if not isinstance(owner, int) or owner <= 1:
        return False
    try:
        command = Path(f"/proc/{owner}/cmdline").read_bytes().replace(b"\0", b" ")
        return b"ai-cctv-edge" in command and b"repair" in command
    except OSError:
        return False


def _active_units() -> list[str]:
    return [unit for unit in UNITS if _systemctl("is-active", "--quiet", unit, check=False).returncode == 0]


def _restore_unit_set(expected: list[str]) -> None:
    for unit in UNITS:
        if unit in expected:
            _systemctl("start", unit)
    for unit in reversed(UNITS):
        if unit not in expected and _systemctl("is-active", "--quiet", unit, check=False).returncode == 0:
            _systemctl("stop", unit)
    actual = _active_units()
    if set(actual) != set(expected):
        raise RuntimeError("service state differs from its pre-Repair state")


@contextmanager
def repair_lock(root: Path = REPAIR_ROOT) -> Iterator[None]:
    if os.name != "posix" or os.geteuid() != 0:
        raise PermissionError("Repair requires root on Linux")
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chown(root, 0, 0)
    os.chmod(root, 0o700)
    path = root / "repair.lock"
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        import fcntl

        os.fchown(fd, 0, 0)
        os.fchmod(fd, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RepairLockBusy("another Edge configuration or Repair operation is running") from exc
        yield
    finally:
        os.close(fd)


def _targets(config: EdgeConfig, config_path: Path) -> list[Path]:
    candidates = [
        config_path,
        config_path.parent / ".configured",
        config.rtsp.password_file,
        config.recovery.token_file,
        config.control.token_file,
        default_state_root() / "video-profile.json",
    ]
    result: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        path = Path(os.path.abspath(candidate))
        if path not in seen:
            seen.add(path)
            result.append(path)
    return result


def _snapshot(paths: list[Path], backup: Path) -> list[dict]:
    files: list[dict] = []
    for index, path in enumerate(paths):
        if path.is_symlink():
            raise ValueError(f"Repair will not replace a symbolic link: {path}")
        entry = {"path": str(path), "existed": path.exists()}
        if path.exists():
            info = path.stat()
            if not stat.S_ISREG(info.st_mode):
                raise ValueError(f"Repair target is not a regular file: {path}")
            saved = backup / f"file-{index:03d}"
            shutil.copyfile(path, saved)
            os.chmod(saved, 0o600)
            with saved.open("r+b") as handle:
                os.fsync(handle.fileno())
            entry.update({"backup": saved.name, "mode": stat.S_IMODE(info.st_mode), "uid": info.st_uid, "gid": info.st_gid})
        files.append(entry)
    _sync_dir(backup)
    return files


def _restore(journal: dict, config_path: Path, *, start_services: bool = True) -> None:
    backup = Path(journal["backup_dir"])
    marker = config_path.parent / ".configured"
    marker.unlink(missing_ok=True)
    for entry in journal["files"]:
        path = Path(entry["path"])
        if path == marker:
            continue
        if entry["existed"]:
            source = backup / entry["backup"]
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_name(f".{path.name}.repair-restore")
            shutil.copyfile(source, temp)
            os.chmod(temp, entry["mode"])
            os.chown(temp, entry["uid"], entry["gid"])
            os.replace(temp, path)
            _sync_dir(path.parent)
        else:
            path.unlink(missing_ok=True)
            if path.parent.exists():
                _sync_dir(path.parent)
    old_config = EdgeConfig.load(config_path)
    apply_persistent(old_config)
    marker_entry = next(item for item in journal["files"] if Path(item["path"]) == marker)
    if marker_entry["existed"]:
        shutil.copyfile(backup / marker_entry["backup"], marker)
        os.chmod(marker, marker_entry["mode"])
        os.chown(marker, marker_entry["uid"], marker_entry["gid"])
        _sync_dir(marker.parent)
    if start_services:
        _restore_unit_set(journal["active_units"])


def _recover_pending(config_path: Path, root: Path, *, start_services: bool) -> bool:
    journal_path = root / "journal.json"
    if not journal_path.exists():
        return False
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    if journal.get("phase") in {"COMMITTED", "ROLLED_BACK"}:
        backup = Path(journal["backup_dir"])
        if backup.exists():
            shutil.rmtree(backup)
        journal_path.unlink(missing_ok=True)
        return False
    journal["phase"] = "ROLLING_BACK"
    _write_journal(journal_path, journal)
    try:
        _systemctl("stop", *UNITS, check=False)
        _restore(journal, config_path, start_services=start_services)
        journal["phase"] = "ROLLED_BACK"
        _write_journal(journal_path, journal)
    except Exception as exc:
        journal["phase"] = "RECOVERY_REQUIRED"
        journal["error"] = str(exc)[:300]
        _write_journal(journal_path, journal)
        raise RuntimeError(f"Repair recovery is incomplete; protected backup remains at {journal['backup_dir']}") from exc
    backup = Path(journal["backup_dir"])
    if backup.exists():
        shutil.rmtree(backup)
    journal_path.unlink(missing_ok=True)
    return True


def recover_pending(
    config_path: Path,
    root: Path = REPAIR_ROOT,
    *,
    start_services: bool = True,
    lock_held: bool = False,
) -> bool:
    if lock_held:
        return _recover_pending(config_path, root, start_services=start_services)
    try:
        with repair_lock(root):
            return _recover_pending(config_path, root, start_services=start_services)
    except RepairLockBusy:
        journal_path = root / "journal.json"
        if start_services or not journal_path.exists():
            raise
        try:
            journal = json.loads(journal_path.read_text(encoding="utf-8"))
            phase = journal.get("phase")
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise
        if phase == "COMMITTED" or (
            phase in {"LOCAL_CONFIGURED", "ROLLING_BACK"}
            and _repair_owner_is_running(journal)
        ):
            return False
        raise


def repair_pairing(config_path: Path, *, device_id: str, camera_id: str, interface: str, run_pairing) -> int:
    config_path = config_path.resolve()
    if config_path != Path("/etc/ai-cctv-edge/config.toml"):
        raise ValueError("Repair requires /etc/ai-cctv-edge/config.toml")
    with repair_lock():
        if PACKAGE_TRANSITION.exists():
            raise RuntimeError("Edge package upgrade is in progress; retry Repair after it completes")
        recover_pending(config_path, lock_held=True)
        old_config = EdgeConfig.load(config_path)
        if old_config.rtsp.mode != "central_publish":
            raise ValueError("Repair currently supports central_publish configurations only")
        paths = _targets(old_config, config_path)
        invalidate_startup()
        selected_network = selected_interface_ipv4(interface)
        for name, host in (("control", old_config.control.bind_host), ("recovery", old_config.recovery.bind_host)):
            if host not in {"0.0.0.0", selected_network.address}:
                raise ValueError(f"{name}.bind_host must be updated before Repair")
        backup = REPAIR_ROOT / str(uuid.uuid4())
        backup.mkdir(mode=0o700)
        active = _active_units()
        journal_path = REPAIR_ROOT / "journal.json"
        try:
            files = _snapshot(paths, backup)
            journal = {
                "phase": "PREPARED",
                "backup_dir": str(backup),
                "files": files,
                "active_units": active,
                "owner_pid": os.getpid(),
                "repair_job_id": str(uuid.uuid4()),
            }
            _write_journal(journal_path, journal)
        except Exception:
            shutil.rmtree(backup, ignore_errors=True)
            raise
        try:
            _systemctl("stop", *UNITS)
            journal["phase"] = "SERVICES_STOPPED"
            _write_journal(journal_path, journal)
            (config_path.parent / ".configured").unlink(missing_ok=True)
            journal["phase"] = "PAIRING"
            _write_journal(journal_path, journal)
            result = run_pairing(old_config, device_id, camera_id, interface)
            if result != 0:
                raise RuntimeError("pairing did not complete")
            updated_config = EdgeConfig.load(config_path)
            store = ProfileSelectionStore(default_state_root())
            _old_profile, generation = store.read(updated_config.video.profile)
            store.write(updated_config.video.profile, generation + 1)
            import pwd

            account = pwd.getpwnam("ai-cctv-edge")
            os.chown(store.path, account.pw_uid, account.pw_gid)
            journal["phase"] = "LOCAL_CONFIGURED"
            _write_journal(journal_path, journal)
            _restore_unit_set(active)
            journal["phase"] = "COMMITTED"
            _write_journal(journal_path, journal)
        except BaseException:
            try:
                _systemctl("stop", *UNITS, check=False)
                journal["phase"] = "ROLLING_BACK"
                _write_journal(journal_path, journal)
                _restore(journal, config_path)
            except Exception as restore_error:
                journal["phase"] = "RECOVERY_REQUIRED"
                journal["error"] = str(restore_error)[:300]
                _write_journal(journal_path, journal)
                print(f"Repair failed; local recovery is incomplete. Protected backup: {backup}")
                return 1
            journal["phase"] = "ROLLED_BACK"
            _write_journal(journal_path, journal)
            if backup.exists():
                shutil.rmtree(backup)
            journal_path.unlink(missing_ok=True)
            raise
        shutil.rmtree(backup)
        journal_path.unlink(missing_ok=True)
        print(f"Repair completed: device={device_id} camera={camera_id} interface={interface}")
        return 0
