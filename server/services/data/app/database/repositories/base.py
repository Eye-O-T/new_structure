# SQL 조회 행을 API용 값으로 바꾸고 저장 계층에서 함께 쓰는 오류와 시간을 정의한다.

from __future__ import annotations

import json
import sqlite3
from typing import Any

from ai_cctv_core.time import format_utc, utc_now

RECOVERY_EVENT_CORRELATION_SECONDS = 60


class CameraLimitReached(Exception):
    pass


class CameraHasHistory(Exception):
    pass


def _now() -> str:
    return format_utc(utc_now())


def _as_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return None if row is None else dict(row)


def _user(row: sqlite3.Row | None) -> dict[str, Any] | None:
    result = _as_dict(row)
    if result is not None:
        result["is_active"] = bool(result["is_active"])
    return result


def _camera(row: sqlite3.Row | None) -> dict[str, Any] | None:
    result = _as_dict(row)
    if result is not None:
        result["enabled"] = bool(result["enabled"])
    return result


# 저장용 JSON과 SQLite 정수를 호출자가 쓰는 프로필 목록·불리언으로 복원한다.
def _video_profile(row: sqlite3.Row | None) -> dict[str, Any] | None:
    result = _as_dict(row)
    if result is not None:
        result["supported_profiles"] = json.loads(result.pop("supported_profiles_json"))
        if "edge_online" in result:
            result["edge_online"] = bool(result["edge_online"])
    return result


# 미관측 상태의 None은 유지하여 명시적으로 관측한 offline과 구분한다.
def _runtime_status(row: sqlite3.Row | None) -> dict[str, Any] | None:
    result = _as_dict(row)
    if result is not None and "online" in result:
        result["online"] = bool(result["online"])
        if result.get("online_observed") is not None:
            result["online_observed"] = bool(result["online_observed"])
    return result


# DB의 metadata_json 필드를 공개 자료 구조의 metadata 객체로 변환한다.
def _event(row: sqlite3.Row | None) -> dict[str, Any] | None:
    result = _as_dict(row)
    if result is not None:
        result["metadata"] = json.loads(result.pop("metadata_json"))
    return result
