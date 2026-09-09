# 서버와 카메라의 지역이 달라도 시간을 비교할 수 있도록 공통 기준인 UTC로 주고받는다.
# 화면에 표시할 때의 한국 시간 변환과, 저장·통신에 쓰는 시간 형식은 구분한다.

from __future__ import annotations

import re
from datetime import UTC, datetime


_CENTRAL_RECORDING_FILENAME = re.compile(
    r"(?P<date>[0-9]{8})T(?P<time>[0-9]{6})-(?P<fraction>[0-9]{1,9})Z\.mp4"
)


def utc_now() -> datetime:
    return datetime.now(UTC)


# ISO 문자열 또는 시간대가 있는 datetime을 받아 동일한 UTC 시점으로 정규화한다.
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


def central_recording_start(filename: str) -> datetime | None:
    """MediaMTX 파일명의 UTC 시작을 읽고, 다른 형식·잘못된 날짜는 None을 반환한다."""
    match = _CENTRAL_RECORDING_FILENAME.fullmatch(filename)
    if match is None:
        return None
    # 파일명의 소수 초를 datetime이 지원하는 마이크로초 여섯 자리로 맞춘다.
    fraction = match.group("fraction")[:6].ljust(6, "0")
    try:
        return datetime.strptime(
            f"{match.group('date')}{match.group('time')}{fraction}",
            "%Y%m%d%H%M%S%f",
        ).replace(tzinfo=UTC)
    except ValueError:
        return None
