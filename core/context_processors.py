from .models import SystemSetting


def app_context(request):
    ctx = {"settings_obj": None}
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
