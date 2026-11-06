"""Formatting for timestamps shown in the desktop monitoring UI."""

from datetime import datetime, timedelta, timezone


_KST = timezone(timedelta(hours=9), name="KST")
_INVALID_TIME = "시각 확인 불가"


def format_kst(value):
    """Render a timezone-aware ISO timestamp in Korea Standard Time."""
    if value is None:
        return _INVALID_TIME
    if value == "-" or value == "":
        return "-"
    try:
        stamp = value if isinstance(value, datetime) else datetime.fromisoformat(
            str(value).replace("Z", "+00:00")
        )
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            return _INVALID_TIME
        return stamp.astimezone(_KST).strftime("%Y-%m-%d %H:%M:%S KST")
    except (TypeError, ValueError, OverflowError):
        return _INVALID_TIME
