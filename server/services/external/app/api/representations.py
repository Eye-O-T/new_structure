# Data의 사용자 정보에서 비밀번호 해시 등 비공개 값을 빼고 공개 응답을 만든다.

from typing import Any

from ..clients.data import DataServiceError


# 허용 목록에 명시한 필드만 남겨 Data에 새 비공개 필드가 추가되어도 응답에 섞이지 않게 한다.
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


# 여러 응답 포맷의 사용자 객체를 같은 공개 필드 필터로 변환한다.
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
