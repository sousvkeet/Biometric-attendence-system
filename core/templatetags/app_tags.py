from django import template
from django.utils import timezone
from django.utils.html import format_html

from core.timefmt import fmt_datetime, fmt_time

register = template.Library()


@register.filter
def get_item(mapping, key):
    if mapping is None:
        return None
    try:
        return mapping.get(key)
    except AttributeError:
        return None


@register.filter
def minutes(value):
    if not value:
        return ""
    value = int(value)
    return f"{value // 60}:{value % 60:02d}"


@register.filter
def hm(value):
    """Time as 18:05 or 6:05 PM (Settings → Time format)."""
    return fmt_time(value)


@register.filter
def hms(value):
    """Time with seconds as 18:05:09 or 6:05:09 PM."""
    return fmt_time(value, seconds=True)


@register.filter
def dtime(value, date_format="d M Y"):
    """Date + time, e.g. {{ x|dtime:"d M Y" }} -> '01 Oct 2026 6:05 PM'."""
    return fmt_datetime(value, date_format)


@register.filter
def dtimes(value, date_format="d M Y"):
    """Date + time with seconds."""
    return fmt_datetime(value, date_format, seconds=True)


@register.simple_tag
def status_badge(status):
    colors = {
        "online": "success", "offline": "danger", "unknown": "secondary",
        "pending": "secondary", "running": "primary", "success": "success", "partial": "warning",
        "failed": "danger", "cancelled": "secondary", "approved": "success", "rejected": "danger",
        "active": "success", "inactive": "secondary",
    }
    return format_html('<span class="badge text-bg-{}">{}</span>', colors.get(status, "secondary"), status.capitalize())


@register.filter
def since(value):
    if not value:
        return "never"
    delta = timezone.now() - value
    secs = int(delta.total_seconds())
    if secs < 60:
        return "just now"
    if secs < 3600:
        return f"{secs // 60} min ago"
    if secs < 86400:
        return f"{secs // 3600} h ago"
    return f"{secs // 86400} d ago"


@register.simple_tag(takes_context=True)
def query_replace(context, **kwargs):
    """Rebuild the current query string replacing some parameters (for pagination / exports)."""
    q = context["request"].GET.copy()
    for k, v in kwargs.items():
        if v is None or v == "":
            q.pop(k, None)
        else:
            q[k] = str(v)
    return q.urlencode()
