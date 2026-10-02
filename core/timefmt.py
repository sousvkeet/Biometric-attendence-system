"""Time display according to Settings → Time format (24-hour or 12-hour AM/PM)."""
import datetime as dt
import time

from django.utils import dateformat, timezone

_cache = {"value": None, "at": 0.0}
CACHE_SECONDS = 10


def reset_cache():
    _cache["value"] = None


def is_12h():
    if _cache["value"] is None or time.monotonic() - _cache["at"] > CACHE_SECONDS:
        from .models import SystemSetting
        try:
            _cache["value"] = SystemSetting.get().time_format == "12"
        except Exception:  # database not ready (migrations)
            return False
        _cache["at"] = time.monotonic()
    return _cache["value"]


def fmt_time(value, seconds=False):
    """'18:05' / '18:05:09' or '6:05 PM' / '6:05:09 PM'. Accepts datetime or time; '' for None."""
    if not value:
        return ""
    if isinstance(value, dt.datetime) and timezone.is_aware(value):
        value = timezone.localtime(value)
    if is_12h():
        hour = value.hour % 12 or 12
        text = f"{hour}:{value.minute:02d}" + (f":{value.second:02d}" if seconds else "")
        return f"{text} {'AM' if value.hour < 12 else 'PM'}"
    return f"{value.hour:02d}:{value.minute:02d}" + (f":{value.second:02d}" if seconds else "")


def fmt_datetime(value, date_format="d M Y", seconds=False):
    """Date (Django date format) followed by the time in the chosen format."""
    if not value:
        return ""
    if timezone.is_aware(value):
        value = timezone.localtime(value)
    return f"{dateformat.format(value, date_format)} {fmt_time(value, seconds)}"
