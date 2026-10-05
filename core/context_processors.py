from django.conf import settings

from .models import SystemSetting

LOCAL_ADDRESSES = ("127.0.0.1", "::1")


def app_context(request):
    # Back / refresh buttons: only in the desktop app's own window (it has no browser toolbar). Other PCs that open
    # the portal over the network use a normal browser with its own buttons.
    ctx = {"settings_obj": None,
           "desktop_window": settings.DESKTOP_MODE and request.META.get("REMOTE_ADDR") in LOCAL_ADDRESSES}
    try:
        s = SystemSetting.get()
    except Exception:  # database not migrated yet
        return ctx
    ctx["settings_obj"] = s
    ctx["company_name"] = s.company_name
    ctx["time_12h"] = s.time_format == "12"
    user = getattr(request, "user", None)
    if user is not None and user.is_authenticated and user.is_staff:
        from attendance.models import LeaveRequest, RegularizationRequest
        from devices.models import Device, DeviceJob
        ctx["nav_counts"] = {
            "leaves": LeaveRequest.objects.filter(status="pending").count(),
            "regularizations": RegularizationRequest.objects.filter(status="pending").count(),
            "offline_devices": Device.objects.filter(is_active=True, status="offline").count(),
            "running_jobs": DeviceJob.objects.filter(status__in=["pending", "running"]).count(),
        }
        ctx["worker_online"] = s.worker_online
    return ctx
