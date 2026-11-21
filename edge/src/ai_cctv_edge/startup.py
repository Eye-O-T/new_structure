"""Root-published, read-only authorization for Edge service startup."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .config import EdgeConfig
from .network_policy import POLICY_DIR, verify_persistent

try:
    import pwd
except ImportError:  # pragma: no cover - Windows development environment
    pwd = None  # type: ignore[assignment]

STARTUP_PATH = POLICY_DIR / "startup.json"
ENTRYPOINTS = {"run", "serve-control", "serve-recovery"}


def _boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="ascii").strip()


def _digest(config_path: Path) -> str:
    return hashlib.sha256(config_path.read_bytes()).hexdigest()


def _repair_allows_start() -> tuple[str, int] | None:
    from .repair import PACKAGE_TRANSITION, REPAIR_ROOT, _repair_owner_is_running

    if PACKAGE_TRANSITION.exists():
        raise RuntimeError("Edge package upgrade is in progress")
    journal_path = REPAIR_ROOT / "journal.json"
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Repair journal is unreadable; recovery is required") from exc
    phase = journal.get("phase")
    if phase in {"COMMITTED", "ROLLED_BACK"}:
        return None
    job_id = journal.get("repair_job_id")
    if (
        phase in {"LOCAL_CONFIGURED", "ROLLING_BACK"}
        and isinstance(job_id, str)
        and _repair_owner_is_running(journal)
    ):
        return job_id, journal["owner_pid"]
    raise RuntimeError(f"Repair recovery must finish before Edge services can start (phase={phase!r})")


def verify_startup(config_path: Path, entrypoint: str) -> None:
    """Privileged preflight: verify private state and publish service-safe status."""
    if os.name != "posix" or os.geteuid() != 0:
        raise PermissionError("startup verification requires root")
    if entrypoint not in ENTRYPOINTS:
        raise ValueError("invalid Edge service entrypoint")
    STARTUP_PATH.unlink(missing_ok=True)
    repair_owner = _repair_allows_start()
    config = EdgeConfig.load(config_path)
    verify_persistent(config)
    if pwd is None:
        raise RuntimeError("Edge startup verification is only supported on Linux")
    account = pwd.getpwnam("ai-cctv-edge")
    payload = {
        "version": 1,
        "allowed": True,
        "entrypoints": sorted(ENTRYPOINTS),
        "config_sha256": _digest(config_path),
        "boot_id": _boot_id(),
        "repair_job_id": repair_owner[0] if repair_owner else None,
        "repair_owner_pid": repair_owner[1] if repair_owner else None,
    }
    temporary = STARTUP_PATH.with_name(f".startup-{os.getpid()}.tmp")
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chown(temporary, 0, account.pw_gid)
        os.chmod(temporary, 0o440)
        os.replace(temporary, STARTUP_PATH)
        descriptor = os.open(STARTUP_PATH.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        temporary.unlink(missing_ok=True)


def invalidate_startup() -> None:
    """Fail closed before Repair changes configuration or policy."""
    if os.name != "posix" or os.geteuid() != 0:
        raise PermissionError("startup authorization changes require root")
    STARTUP_PATH.unlink(missing_ok=True)


def require_startup(config_path: Path, entrypoint: str) -> None:
    """Unprivileged service check; never reads private Repair paths."""
    if entrypoint not in ENTRYPOINTS:
        raise ValueError("invalid Edge service entrypoint")
    try:
        payload = json.loads(STARTUP_PATH.read_text(encoding="utf-8"))
        current_boot = _boot_id()
        current_digest = _digest(config_path)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Edge startup authorization is missing or invalid; root preflight required") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("version") != 1
        or payload.get("allowed") is not True
        or entrypoint not in payload.get("entrypoints", [])
        or payload.get("config_sha256") != current_digest
        or payload.get("boot_id") != current_boot
    ):
        raise RuntimeError("Edge startup authorization does not match this service, config, or boot")
    if payload.get("repair_job_id") is not None:
        from .repair import _repair_owner_is_running

        if not _repair_owner_is_running({"owner_pid": payload.get("repair_owner_pid")}):
            raise RuntimeError("Edge Repair restart authorization is stale")
    verify_persistent(EdgeConfig.load(config_path))
