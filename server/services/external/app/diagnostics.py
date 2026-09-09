"""내부 상태 응답에서 공개 가능한 진단 코드·수치만 골라 관리자에게 전달한다."""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone
from typing import Any

import httpx

from .config import CAMERA_ID_PATTERN, Settings


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> int | float | None:
    return (
        value
        if (
            not isinstance(value, bool)
            and isinstance(value, (int, float))
            and math.isfinite(value)
            and value >= 0
        )
        else None
    )


def _timestamp(value: Any) -> str | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).isoformat() if parsed.tzinfo else None
    except (AttributeError, TypeError, ValueError, OverflowError):
        return None


def _state(value: Any, allowed: set[str]) -> str:
    return value if isinstance(value, str) and value in allowed else "unknown"


def _failure(code: str) -> dict[str, Any]:
    return {"status": "unavailable", "last_error_code": code}


def public_data_status(payload: dict[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {
        "status": _state(payload.get("status"), {"ready", "degraded"}),
        "workers": {},
        "queues": {},
        "queue_metrics": {},
    }
    database = _object(payload.get("database"))
    if database:
        result["database"] = {
            "foreign_keys": database.get("foreign_keys") is True,
            "journal_mode": _state(
                database.get("journal_mode"),
                {"wal", "delete", "truncate", "persist", "memory", "off"},
            ),
        }
    for name in ("recovery", "storage"):
        worker = _object(_object(payload.get("workers")).get(name))
        if worker:
            result["workers"][name] = {
                "alive": worker.get("alive") is True,
                "status": _state(worker.get("status"), {"starting", "ok", "error"}),
                "last_success_at": _timestamp(worker.get("last_success_at")),
                "last_error_code": "WORKER_ERROR" if worker.get("last_error") else None,
            }
    queue_states = {
        "identity": ("pending", "running", "failed", "unconfigured", "complete"),
        "analysis": ("pending", "running", "failed", "unconfigured", "complete"),
        "recovery": (
            "detected",
            "waiting_for_recovery",
            "downloading",
            "indexing",
            "completed",
            "failed",
        ),
        "push": ("pending", "sending", "sent", "failed", "cancelled"),
    }
    for name, states in queue_states.items():
        queue = _object(_object(payload.get("queues")).get(name))
        result["queues"][name] = {state: _number(queue.get(state)) for state in states}
        metrics = _object(_object(payload.get("queue_metrics")).get(name))
        result["queue_metrics"][name] = {
            "oldest_pending_seconds": _number(metrics.get("oldest_pending_seconds")),
            "last_success_at": _timestamp(metrics.get("last_success_at")),
            "last_failure_at": _timestamp(metrics.get("last_failure_at")),
        }
    storage = _object(payload.get("storage"))
    fields = (
        "total_bytes",
        "used_bytes",
        "free_bytes",
        "free_percent",
        "warning_below_percent",
    )
    result["storage"] = {field: _number(storage.get(field)) for field in fields}
    result["storage"]["volumes"] = {}
    for name in ("recordings", "snapshots", "database", "backups"):
        volume = _object(_object(storage.get("volumes")).get(name))
        if volume:
            result["storage"]["volumes"][name] = {
                "status": _state(
                    volume.get("status"),
                    {"ok", "ready", "warning", "error", "unavailable"},
                ),
                **{field: _number(volume.get(field)) for field in fields},
            }
    retention = _object(payload.get("retention"))
    if retention:
        result["retention"] = {
            "status": _state(retention.get("status"), {"ready", "deferred"}),
            "last_error_code": "RETENTION_DEFERRED"
            if retention.get("status") == "deferred"
            else None,
            "generated_at": _timestamp(retention.get("generated_at")),
            "protected_paths": _number(retention.get("protected_paths")),
            "protected_events": _number(retention.get("protected_events")),
        }
    if (
        any(
            volume["status"] in {"warning", "error", "unavailable"}
            for volume in result["storage"]["volumes"].values()
        )
        or retention.get("status") == "deferred"
        or any(
            not worker["alive"] or worker["status"] == "error"
            for worker in result["workers"].values()
        )
    ):
        result["status"] = "degraded"
    return result


def public_preprocessing_status(payload: dict[str, Any]) -> dict[str, Any]:
    identity = _object(payload.get("identity"))
    delivery = _object(payload.get("event_delivery"))
    cameras = []
    for camera_id, raw in _object(payload.get("workers")).items():
        if not isinstance(camera_id, str) or not CAMERA_ID_PATTERN.fullmatch(camera_id):
            continue
        worker = _object(raw)
        cameras.append(
            {
                "camera_id": camera_id,
                "state": _state(
                    worker.get("state"),
                    {"starting", "running", "online", "offline", "stopped", "error"},
                ),
                "model_ready": worker.get("model_ready") is True,
                "frame_stale": worker.get("frame_stale") is True,
                "frame_age_seconds": _number(worker.get("frame_age_seconds")),
                "event_persistence_failures": _number(
                    worker.get("event_persistence_failures")
                ),
                "event_shutdown_losses": _number(worker.get("event_shutdown_losses")),
                "model_timeouts": _number(worker.get("model_timeouts")),
                "last_inference_seconds": _number(worker.get("last_inference_seconds")),
                "observation_candidates": _number(worker.get("observation_candidates")),
                "observation_buffer_bytes": _number(
                    worker.get("observation_buffer_bytes")
                ),
                "last_error_code": "CAMERA_WORKER_ERROR"
                if worker.get("last_error")
                else None,
            }
        )
    return {
        "status": _state(payload.get("status"), {"ready", "degraded"}),
        "data_ready": payload.get("data_ready") is True,
        "cameras": cameras,
        "identity": {
            "ready": identity.get("ready") is True,
            "model_ready": identity.get("model_ready") is True,
            "stalled": identity.get("stalled") is True,
            "last_error_code": "IDENTITY_WORKER_ERROR"
            if identity.get("last_error")
            else None,
        },
        "event_delivery": {
            "pending": _number(delivery.get("pending")),
            "rejected": _number(delivery.get("rejected")),
            "waiting": _number(delivery.get("waiting")),
            "last_error_code": "EVENT_DELIVERY_ERROR"
            if delivery.get("last_error")
            else None,
        },
    }


async def system_diagnostics(
    settings: Settings, data: Any, *, transport=None
) -> dict[str, Any]:
    """실패한 서비스 하나가 다른 서비스의 상세 진단까지 지우지 않게 한다."""
    async with httpx.AsyncClient(
        timeout=settings.system_probe_timeout_seconds,
        trust_env=False,
        follow_redirects=False,
        transport=transport,
    ) as client:

        async def data_probe():
            try:
                payload = await asyncio.wait_for(
                    data.health_status(), timeout=settings.system_probe_timeout_seconds
                )
                return public_data_status(payload)
            except Exception:
                return _failure("DATA_UNAVAILABLE")

        async def preprocessing_probe():
            try:
                response = await client.get(settings.preprocessing_health_url)
                payload = response.json()
                if response.status_code not in {200, 503} or not isinstance(
                    payload, dict
                ):
                    raise ValueError("invalid health response")
                if payload.get("status") not in {"ready", "degraded"}:
                    raise ValueError("invalid health status")
                return public_preprocessing_status(payload)
            except Exception:
                return _failure("PREPROCESSING_UNAVAILABLE")

        async def media_probe():
            try:
                response = await client.get(
                    settings.media_control_url.rstrip("/") + "/v3/paths/list",
                    params={"itemsPerPage": 1000, "page": 0},
                )
                response.raise_for_status()
                payload = response.json()
                items = payload.get("items")
                total = _number(payload.get("itemCount"))
                if not isinstance(items, list) or total is None:
                    raise ValueError("invalid media response")
                partial = total > len(items)
                return {
                    "status": "ready",
                    "path_count": total,
                    "active_publishers": None
                    if partial
                    else sum(
                        _object(item).get("ready") is True
                        and bool(_object(item).get("source"))
                        for item in items
                    ),
                    "partial": partial,
                }
            except Exception:
                return _failure("MEDIA_UNAVAILABLE")

        # 전체 회차에도 기한을 적용해 다수 청크를 천천히 보내는 응답에 계속 기다리지 않는다.
        async def bounded(probe, code):
            try:
                return await asyncio.wait_for(
                    probe(), settings.system_probe_timeout_seconds
                )
            except Exception:
                return _failure(code)

        results = await asyncio.gather(
            bounded(data_probe, "DATA_UNAVAILABLE"),
            bounded(preprocessing_probe, "PREPROCESSING_UNAVAILABLE"),
            bounded(media_probe, "MEDIA_UNAVAILABLE"),
        )
        return dict(zip(("data", "preprocessing", "media"), results))
