"""Periodic central collection of Edge runtime state and durable event journals."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Callable

from .config import Settings
from .data_client import DataClient
from .edge_client import EdgeControlError, EdgeHttpClient


LOGGER = logging.getLogger("ai_cctv.external.status_collector")


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


@asynccontextmanager
async def _unlocked_camera():
    yield


class StatusCollector:
    def __init__(
        self,
        *,
        settings: Settings,
        data_client: DataClient,
        edge_client_factory: Callable[[dict[str, Any]], EdgeHttpClient] | None = None,
        camera_lock_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.settings = settings
        self.data = data_client
        self._edge_client_factory = edge_client_factory or self._new_edge_client
        self._camera_lock_factory = camera_lock_factory or (
            lambda _camera_id: _unlocked_camera()
        )

    def _new_edge_client(self, target: dict[str, Any]) -> EdgeHttpClient:
        return EdgeHttpClient(
            base_url=str(target["management_url"]),
            auth_token=str(target["auth_token"]),
            timeout_seconds=self.settings.edge_status_timeout_seconds,
        )

    @staticmethod
    def _targets(payload: Any) -> list[dict[str, Any]]:
        items = payload.get("items", []) if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            raise ValueError("Data Service returned invalid control targets")
        return [item for item in items if isinstance(item, dict)]

    @staticmethod
    def _status(camera_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        returned_camera = payload.get("camera_id")
        if returned_camera is not None and str(returned_camera) != camera_id:
            raise EdgeControlError(
                "INVALID_EDGE_RESPONSE", "The Edge status camera ID did not match."
            )
        online = payload.get("online")
        if not isinstance(online, bool):
            raise EdgeControlError(
                "INVALID_EDGE_RESPONSE", "The Edge online state is invalid."
            )
        camera_input = payload.get(
            "camera_input", payload.get("camera_input_status", "unknown")
        )
        if camera_input not in {"online", "offline", "lost"}:
            camera_input = "unknown"
        central_status = payload.get("central_connection_status", "unknown")
        if central_status not in {"online", "offline"}:
            central_status = "unknown"
        current_profile = payload.get(
            "current_video_profile", payload.get("current_profile")
        )
        if current_profile not in {"hd", "fhd"}:
            raise EdgeControlError(
                "INVALID_EDGE_RESPONSE", "The Edge video profile is invalid."
            )
        for field in (
            "cpu_percent",
            "memory_percent",
            "storage_percent",
            "battery_percent",
        ):
            value = payload.get(field)
            if value is not None and (
                not isinstance(value, (int, float)) or not 0 <= value <= 100
            ):
                raise EdgeControlError(
                    "INVALID_EDGE_RESPONSE", f"The Edge {field} value is invalid."
                )
        return {
            "_capture_state": payload.get("capture_state", "unknown"),
            "online": online,
            "cpu_percent": payload.get("cpu_percent"),
            "memory_percent": payload.get("memory_percent"),
            "storage_percent": payload.get("storage_percent"),
            "battery_percent": payload.get("battery_percent"),
            "power_source": payload.get("power_source", "unknown"),
            "camera_input": camera_input,
            "central_connection_status": central_status,
            "current_video_profile": current_profile,
            "last_seen_at": payload.get("last_seen_at") or _utc_now(),
            "last_error_code": payload.get("last_error_code"),
        }

    @staticmethod
    def _profile_observation(payload: dict[str, Any]) -> dict[str, Any]:
        current = payload.get("current_video_profile", payload.get("current_profile"))
        observation: dict[str, Any] = {"current_profile": current}
        encoder = payload.get("encoder")
        if isinstance(encoder, str) and encoder:
            observation["encoder"] = encoder

        # Status from older Edge releases contained configured declarations,
        # not probed sensor capabilities. Only an explicitly available probe
        # may replace the central supported-profile set.
        if payload.get("capability_status") != "available":
            return observation
        supported = payload.get(
            "supported_profiles", payload.get("supported_video_profiles")
        )
        if supported is None:
            supported = [current]
        if (
            not isinstance(supported, list)
            or not supported
            or any(profile not in {"hd", "fhd"} for profile in supported)
        ):
            raise EdgeControlError(
                "INVALID_EDGE_RESPONSE",
                "The Edge supported video profiles are invalid.",
            )
        observation["supported_profiles"] = list(dict.fromkeys(supported))
        return observation

    async def _create_transition_event(
        self,
        camera_id: str,
        event_type: str,
        occurred_at: str,
        baseline: dict[str, Any],
        metadata: dict[str, Any] | None = None,
    ) -> None:
        details = {"source": "central_status_collector"}
        details.update(metadata or {})
        # Runtime is updated only after every synthetic event succeeds. This
        # fingerprint therefore remains stable if event creation or the later
        # runtime write fails, while runtime_updated_at makes a future instance
        # of the same transition distinct after the baseline advances.
        boundary = {
            key: baseline.get(key)
            for key in (
                "runtime_updated_at",
                "last_seen_at",
                "online",
                "camera_input",
                "central_connection_status",
                "power_source",
                "battery_percent",
                "storage_percent",
            )
        }
        boundary_digest = hashlib.sha256(
            json.dumps(boundary, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:32]
        await self.data.create_event(
            {
                "camera_id": camera_id,
                "event_type": event_type,
                "occurred_at": occurred_at,
                "edge_event_id": f"collector:{event_type}:{boundary_digest}",
                "metadata": details,
            }
        )

    async def _drain_events(
        self,
        edge: EdgeHttpClient,
        target: dict[str, Any],
    ) -> tuple[int, set[str], str | None, bool]:
        camera_id = str(target["camera_id"])
        edge_device_id = str(target["edge_device_id"])
        cursor = target.get("event_cursor")
        imported = 0
        imported_types: set[str] = set()
        cursor_expired = False
        for _ in range(100):
            previous_cursor = cursor
            page = await edge.list_events(
                after=None if cursor is None else str(cursor),
                limit=self.settings.edge_event_page_limit,
            )
            items = page.get("items")
            next_cursor = page.get("next_cursor", cursor)
            if page.get("cursor_expired") is True:
                cursor_expired = True
                LOGGER.warning(
                    "Edge event cursor expired; replaying retained journal items",
                    extra={"camera_id": camera_id, "event_cursor": cursor},
                )
            if not isinstance(items, list):
                raise EdgeControlError(
                    "INVALID_EDGE_RESPONSE", "The Edge event journal is invalid."
                )
            for raw in items:
                if not isinstance(raw, dict):
                    raise EdgeControlError(
                        "INVALID_EDGE_RESPONSE", "The Edge event journal is invalid."
                    )
                raw_camera_id = str(raw.get("camera_id", camera_id))
                if raw_camera_id != camera_id:
                    raise EdgeControlError(
                        "INVALID_EDGE_RESPONSE",
                        "The Edge event camera ID did not match.",
                    )
                event_id = raw.get("event_id")
                event_type = raw.get("event_type")
                occurred_at = raw.get("occurred_at")
                if event_id is None or not event_type or not occurred_at:
                    raise EdgeControlError(
                        "INVALID_EDGE_RESPONSE", "The Edge event journal is incomplete."
                    )
                known = {"event_id", "event_type", "camera_id", "occurred_at"}
                metadata = {
                    key: value for key, value in raw.items() if key not in known
                }
                metadata["source"] = "edge_event_journal"
                await self.data.create_event(
                    {
                        "camera_id": camera_id,
                        "event_type": str(event_type),
                        "occurred_at": str(occurred_at),
                        "edge_event_id": f"{edge_device_id}:{event_id}",
                        "metadata": metadata,
                    }
                )
                imported += 1
                imported_types.add(str(event_type))
            if next_cursor is not None and next_cursor != cursor:
                cursor = str(next_cursor)
            if (
                len(items) < self.settings.edge_event_page_limit
                or next_cursor == previous_cursor
            ):
                break
        # Cursor persistence is deliberately deferred until the complete drain
        # succeeds. If a later page fails, replaying already imported events is
        # safe because edge_event_id is idempotent. More importantly, the old
        # runtime transition baseline remains intact for the next poll.
        return (
            imported,
            imported_types,
            None if cursor is None else str(cursor),
            cursor_expired,
        )

    @staticmethod
    def _battery_level(value: Any, settings: Settings) -> str:
        if not isinstance(value, (int, float)):
            return "unknown"
        if value <= settings.battery_critical_percent:
            return "critical"
        if value <= settings.battery_low_percent:
            return "low"
        return "normal"

    @staticmethod
    def _storage_level(value: Any, settings: Settings) -> str:
        if not isinstance(value, (int, float)):
            return "unknown"
        if value >= settings.storage_critical_percent:
            return "critical"
        if value >= settings.storage_warning_percent:
            return "warning"
        return "normal"

    async def _synthesise_missing_transitions(
        self,
        camera_id: str,
        current: dict[str, Any],
        stored: dict[str, Any],
        imported_types: set[str],
    ) -> None:
        occurred_at = str(current["last_seen_at"])
        transitions: list[tuple[str, dict[str, Any]]] = []
        capture_running = current.get("_capture_state") == "running"
        previous_input = stored.get(
            "camera_input", stored.get("previous_camera_input", "unknown")
        )
        current_input = current.get("camera_input", "unknown")
        if (
            capture_running
            and previous_input in {"offline", "lost"}
            and current_input == "online"
        ):
            transitions.append(
                (
                    "camera_input_restored",
                    {"camera_input": current_input},
                )
            )
        elif (
            capture_running
            and previous_input == "online"
            and current_input in {"offline", "lost"}
        ):
            transitions.append(("camera_input_lost", {"camera_input": current_input}))
        previous_central = stored.get(
            "central_connection_status",
            stored.get("previous_central_connection_status", "unknown"),
        )
        current_central = current.get("central_connection_status", "unknown")
        if (
            capture_running
            and previous_central == "offline"
            and current_central == "online"
        ):
            transitions.append(
                (
                    "central_connection_restored",
                    {"central_connection_status": current_central},
                )
            )
        elif (
            capture_running
            and previous_central == "online"
            and current_central == "offline"
        ):
            transitions.append(
                (
                    "central_connection_lost",
                    {"central_connection_status": current_central},
                )
            )
        previous_power = stored.get(
            "power_source", stored.get("previous_power_source", "unknown")
        )
        current_power = current.get("power_source", "unknown")
        if previous_power == "external" and current_power == "battery":
            transitions.append(("external_power_lost", {"power_source": current_power}))
        elif previous_power == "battery" and current_power == "external":
            transitions.append(
                ("external_power_restored", {"power_source": current_power})
            )

        previous_battery = self._battery_level(
            stored.get("battery_percent", stored.get("previous_battery_percent")),
            self.settings,
        )
        current_battery = self._battery_level(
            current.get("battery_percent"), self.settings
        )
        if current_battery != previous_battery and current_battery in {
            "low",
            "critical",
        }:
            transitions.append(
                (
                    f"battery_{current_battery}",
                    {"battery_percent": current.get("battery_percent")},
                )
            )

        previous_storage = self._storage_level(
            stored.get("storage_percent", stored.get("previous_storage_percent")),
            self.settings,
        )
        current_storage = self._storage_level(
            current.get("storage_percent"), self.settings
        )
        if current_storage != previous_storage and current_storage in {
            "warning",
            "critical",
        }:
            transitions.append(
                (
                    f"storage_{current_storage}",
                    {"storage_percent": current.get("storage_percent")},
                )
            )
        for event_type, metadata in transitions:
            if event_type not in imported_types:
                await self._create_transition_event(
                    camera_id, event_type, occurred_at, stored, metadata
                )

    async def _collect_target(self, target: dict[str, Any]) -> tuple[bool, int]:
        camera_id = str(target["camera_id"])
        async with self._camera_lock_factory(camera_id):
            return await self._collect_target_locked(target)

    async def _collect_target_locked(self, target: dict[str, Any]) -> tuple[bool, int]:
        camera_id = str(target["camera_id"])
        edge = self._edge_client_factory(target)
        try:
            stored = await self.data.get_camera_runtime_status(camera_id)
            payload = await edge.get_status()
            normalized = self._status(camera_id, payload)
            runtime_status = {
                key: value
                for key, value in normalized.items()
                if not key.startswith("_")
            }
            await self.data.update_camera_video_profile(
                camera_id, self._profile_observation(payload)
            )
            # Drain the authoritative Edge journal before replacing the
            # runtime snapshot. If the journal is temporarily unavailable, the
            # error path only marks Edge offline and preserves the previous
            # power/input/storage values. The next successful poll can then
            # compare against that baseline and synthesize any truly missing
            # transition without duplicating a journaled event.
            (
                imported,
                imported_types,
                event_cursor,
                cursor_expired,
            ) = await self._drain_events(edge, target)
            if event_cursor is not None:
                runtime_status["event_cursor"] = event_cursor
            if cursor_expired:
                runtime_status["last_error_code"] = "EVENT_CURSOR_EXPIRED"
            previous_online = stored.get("online_observed", stored.get("online"))
            if previous_online is False and normalized["online"]:
                await self._create_transition_event(
                    camera_id,
                    "edge_online",
                    str(normalized["last_seen_at"]),
                    stored,
                )
            elif previous_online is True and not normalized["online"]:
                await self._create_transition_event(
                    camera_id,
                    "edge_offline",
                    str(normalized["last_seen_at"]),
                    stored,
                )
            await self._synthesise_missing_transitions(
                camera_id, normalized, stored, imported_types
            )
            await self.data.put_camera_runtime_status(camera_id, runtime_status)
            return bool(normalized["online"]), imported
        except EdgeControlError as exc:
            occurred_at = _utc_now()
            previous_online = stored.get("online_observed", stored.get("online"))
            if previous_online is True:
                await self._create_transition_event(
                    camera_id, "edge_offline", occurred_at, stored
                )
            await self.data.put_camera_runtime_status(
                camera_id,
                {
                    "online": False,
                    "last_error_code": exc.code,
                },
            )
            return False, 0
        finally:
            await edge.close()

    async def collect_once(self) -> dict[str, int]:
        targets = self._targets(await self.data.list_camera_control_targets())
        results = await asyncio.gather(
            *(self._collect_target(target) for target in targets),
            return_exceptions=True,
        )
        online = 0
        events = 0
        failures = 0
        for result in results:
            if isinstance(result, Exception):
                failures += 1
                LOGGER.error(
                    "Edge status collection failed",
                    exc_info=(type(result), result, result.__traceback__),
                )
            else:
                is_online, imported = result
                online += int(is_online)
                failures += int(not is_online)
                events += imported
        return {
            "targets": len(targets),
            "online": online,
            "failures": failures,
            "events_imported": events,
        }

    async def run(self) -> None:
        while True:
            try:
                await self.collect_once()
            except Exception:
                LOGGER.exception("Edge status collection cycle failed")
            await asyncio.sleep(self.settings.edge_status_poll_interval_seconds)
