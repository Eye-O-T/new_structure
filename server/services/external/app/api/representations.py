# Data의 사용자 정보에서 비밀번호 해시 등 비공개 값을 빼고 공개 응답을 만든다.
"""Public user fields, excluding credentials and private Data attributes."""

from typing import Any

from ..clients.data import DataServiceError


def _public_user(user: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "id",
        "user_id",
        "username",
        "role",
        "is_active",
        "active",
        "created_at",
        "updated_at",
    }
    return {key: value for key, value in user.items() if key in allowed}


def _public_users(payload: Any) -> Any:
    if isinstance(payload, list):
        return [_public_user(item) for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        result = dict(payload)
        result["items"] = [
            _public_user(item) for item in payload["items"] if isinstance(item, dict)
        ]
        return result
    if isinstance(payload, dict):
        return _public_user(payload)
    raise DataServiceError("invalid user response")
