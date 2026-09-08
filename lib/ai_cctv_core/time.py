"""UTC-only time helpers shared across service boundaries."""

# 서버와 카메라의 지역이 달라도 시간을 비교할 수 있도록 공통 기준인 UTC로 주고받는다.
# 화면에 표시할 때의 한국 시간 변환과, 저장·통신에 쓰는 시간 형식은 구분한다.

from __future__ import annotations

from datetime import UTC, datetime


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_utc(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
        parsed = datetime.fromisoformat(normalized)

    if parsed.tzinfo is None:
        # 시간대 없는 값은 어느 지역의 시간인지 판단할 수 없으므로 임의로 추정하지 않는다.
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def format_utc(value: datetime) -> str:
    # 끝의 Z는 UTC라는 뜻이다. 밀리초까지 남겨 서비스마다 출력 형식이 달라지지 않게 한다.
    return parse_utc(value).isoformat(timespec="milliseconds").replace("+00:00", "Z")
