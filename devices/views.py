import datetime as dt

from django.contrib import messages
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.decorators import staff_required
from core.pagination import paginate
from core.models import ActivityLog
from employees.models import Employee

from .drivers import DeviceError, SimulatorSession
from .forms import DeviceForm, SimulatedPunchForm
from .jobs import enqueue
from .models import Device, DeviceJob
from .sync import KEEP_PERIODS, employees_for_device, keep_period, months_before, portal_punches_between

DEVICE_ACTIONS = {
    "refresh": (DeviceJob.TYPE_REFRESH, False, "Connection test"),
    "pull_users": (DeviceJob.TYPE_PULL_USERS, False, "Download users & fingerprints"),
    "preview_push": (DeviceJob.TYPE_PREVIEW_PUSH, False, "Preview upload (dry run)"),
    "push_users": (DeviceJob.TYPE_PUSH_USERS, True, "Upload users & fingerprints"),
    "pull_attendance": (DeviceJob.TYPE_PULL_ATTENDANCE, False, "Download attendance"),
    "sync_time": (DeviceJob.TYPE_SYNC_TIME, True, "Sync clock"),
}
GLOBAL_ACTIONS = {
    "sync_users": (DeviceJob.TYPE_SYNC_USERS, "Sync users & fingerprints across all devices"),
    "pull_attendance": (DeviceJob.TYPE_PULL_ATTENDANCE, "Download attendance from all devices"),
    "refresh": (DeviceJob.TYPE_REFRESH, "Refresh status of all devices"),
    "sync_time": (DeviceJob.TYPE_SYNC_TIME, "Sync clocks of all devices"),
    "push_users": (DeviceJob.TYPE_PUSH_USERS, "Upload all employees to all devices"),
}


@staff_required
def device_list(request):
    devices = Device.objects.all()
    return render(request, "devices/list.html", {
        "devices": devices,
        "keep_periods": _keep_period_choices(None) if request.user.is_superuser else [],
    })


@staff_required
def device_form(request, pk=None):
    device = get_object_or_404(Device, pk=pk) if pk else None
    form = DeviceForm(request.POST or None, instance=device)
    if request.method == "POST" and form.is_valid():
        device = form.save()
        ActivityLog.log(request, "Device " + ("updated" if pk else "added"), device, ", ".join(form.changed_data))
        messages.success(request, f"Device {device.name} saved.")
        if not pk or {"ip_address", "port", "comm_password", "driver"} & set(form.changed_data):
            job = enqueue(DeviceJob.TYPE_REFRESH, device=device, user=request.user)
            messages.info(request, f"Testing connection (job #{job.pk})...")
        return redirect(device)
    return render(request, "devices/form.html", {"form": form, "device": device})


@staff_required
def device_detail(request, pk):
    device = get_object_or_404(Device, pk=pk)
    links = device.user_links.select_related("employee", "employee__department").order_by("uid")
    q = request.GET.get("q", "").strip()
    if q:
        links = links.filter(Q(employee__name__icontains=q) | Q(employee__device_user_id__iexact=q))
    linked_ids = set(device.user_links.values_list("employee_id", flat=True))
    missing = [e for e in employees_for_device(device) if e.pk not in linked_ids]
    return render(request, "devices/detail.html", {
        "device": device,
        "links_page": paginate(request, links, 50),
        "q": q,
        "missing": missing[:50],
        "missing_count": len(missing),
        "jobs": device.jobs.all()[:8],
        "recent_logs": device.logs.select_related("employee").order_by("-punch_time")[:10],
        "log_count": device.logs.count(),
        "actions": DEVICE_ACTIONS,
        "punch_form": SimulatedPunchForm() if device.is_simulator else None,
        "keep_periods": _keep_period_choices(device) if request.user.is_superuser and not device.read_only else [],
    })


def _keep_period_choices(device):
    today = timezone.localdate()
    choices = []
    for code, label, months in KEEP_PERIODS:
        start = months_before(today, months)
        choices.append({"code": code, "label": label, "from": start,
                        "count": portal_punches_between(device, start, today).count()})
    return choices


def _parse_day(value):
    try:
        return dt.date.fromisoformat((value or "").strip())
    except ValueError:
        return None


@staff_required
@require_POST
def device_action(request, pk, action):
    device = get_object_or_404(Device, pk=pk)
    if action not in DEVICE_ACTIONS:
        messages.error(request, "Unknown action.")
        return redirect(device)
    job_type, writes, label = DEVICE_ACTIONS[action]
    if writes and device.read_only:
        messages.error(request, f"{device.name} is write-protected. Turn off read-only mode to {label.lower()}.")
        return redirect(device)
    payload = {"force": True} if action == "pull_attendance" and request.POST.get("force") else {}
    job = enqueue(job_type, device=device, payload=payload, user=request.user)
    ActivityLog.log(request, f"Device: {label}", device)
    messages.success(request, f"{label} queued (job #{job.pk}).")
    return redirect(job)


def _maintenance_running(device=None):
    """A reboot/clear job that is waiting or running for this device (or for several devices)."""
    jobs = DeviceJob.objects.filter(job_type__in=[DeviceJob.TYPE_RESTART, DeviceJob.TYPE_CLEAR_ATTENDANCE],
                                    status__in=["pending", "running"])
    if device is None:
        return jobs.first()
    return jobs.filter(Q(device=device) | Q(device__isnull=True)).first()


@staff_required
@require_POST
def devices_maintenance(request, action):
    """Reboot / clear attendance records on the devices ticked on the Devices page (one after the other)."""
    if action not in ("reboot", "clear"):
        messages.error(request, "Unknown action.")
        return redirect("devices:list")
    ids = [int(v) for v in request.POST.getlist("device") if v.isdigit()]
    devices = list(Device.objects.filter(pk__in=ids).order_by("name"))
    if not devices:
        messages.error(request, "Tick at least one device.")
        return redirect("devices:list")
    period = None
    if action == "clear":
        if not request.user.is_superuser:
            messages.error(request, "Only the main administrator can clear attendance records on devices.")
            return redirect("devices:list")
        protected = [d.name for d in devices if d.read_only]
        if protected:
            messages.error(request, f"Not cleared: {', '.join(protected)} {'is' if len(protected) == 1 else 'are'} "
                                    "write-protected. Untick or turn off read-only mode first.")
            return redirect("devices:list")
        if request.POST.get("confirm_text", "").strip().upper() != "CLEAR":
            messages.error(request, "Not cleared: type CLEAR to confirm.")
            return redirect("devices:list")
        try:
            period = keep_period(request.POST.get("keep", ""), _parse_day(request.POST.get("keep_from")),
                                 _parse_day(request.POST.get("keep_to")))
        except ValueError as exc:
            messages.error(request, f"Not cleared: {exc}")
            return redirect("devices:list")
    busy = _maintenance_running()
    if busy:
        messages.info(request, f"{busy.get_job_type_display()} is already in progress - wait until it finishes.")
        return redirect(busy)
    names = ", ".join(d.name for d in devices)
    if len(devices) == 1:
        job_type = DeviceJob.TYPE_CLEAR_ATTENDANCE if period else DeviceJob.TYPE_RESTART
        job = enqueue(job_type, device=devices[0], payload={"period": period} if period else {}, user=request.user)
    else:
        payload = {"device_ids": [d.pk for d in devices]}
        if period:
            payload["period"] = period
        job = enqueue(DeviceJob.TYPE_CLEAR_ATTENDANCE if period else DeviceJob.TYPE_RESTART, payload=payload,
                      user=request.user)
    if period:
        ActivityLog.log(request, "Devices: clear attendance records", names, f"keep in portal: {period['label']}")
        messages.success(request, f"Clearing attendance records of {names} queued (job #{job.pk}). "
                                  "One device at a time; every record is saved in the portal first.")
    else:
        ActivityLog.log(request, "Devices: reboot", names)
        messages.success(request, f"Reboot of {names} queued (job #{job.pk}). The devices restart one after the "
                                  "other, each offline for about a minute.")
    return redirect(job)


@staff_required
@require_POST
def device_reboot(request, pk):
    device = get_object_or_404(Device, pk=pk)
    busy = _maintenance_running(device)
    if busy:
        messages.info(request, f"{busy.get_job_type_display()} is already in progress for {device.name}.")
        return redirect(busy)
    job = enqueue(DeviceJob.TYPE_RESTART, device=device, user=request.user)
    ActivityLog.log(request, "Device: reboot", device)
    messages.success(request, f"Reboot of {device.name} queued (job #{job.pk}). The device is offline for about a minute.")
    return redirect(job)


@staff_required
@require_POST
def device_clear_attendance(request, pk):
    """Manual only: delete the attendance records on the device after they are all safely in the portal."""
    device = get_object_or_404(Device, pk=pk)
    if not request.user.is_superuser:
        messages.error(request, "Only the main administrator can clear attendance records on a device.")
        return redirect(device)
    if device.read_only:
        messages.error(request, f"{device.name} is write-protected. Turn off read-only mode to clear its records.")
        return redirect(device)
    if request.POST.get("confirm_name", "").strip().casefold() != device.name.strip().casefold():
        messages.error(request, f"Not cleared: type the device name \u201c{device.name}\u201d exactly to confirm.")
        return redirect(device)
    try:
        period = keep_period(request.POST.get("keep", ""), _parse_day(request.POST.get("keep_from")),
                             _parse_day(request.POST.get("keep_to")))
    except ValueError as exc:
        messages.error(request, f"Not cleared: {exc}")
        return redirect(device)
    busy = _maintenance_running(device)
    if busy:
        messages.info(request, f"{busy.get_job_type_display()} is already in progress for {device.name}.")
        return redirect(busy)
    job = enqueue(DeviceJob.TYPE_CLEAR_ATTENDANCE, device=device, payload={"period": period}, user=request.user)
    ActivityLog.log(request, "Device: clear attendance records", device,
                    f"{device.record_count if device.record_count is not None else '?'} records on device; "
                    f"keep in portal: {period['label']}")
    messages.success(request, f"Clearing attendance records of {device.name} queued (job #{job.pk}). "
                              "All records are saved in the portal first.")
    return redirect(job)


@staff_required
@require_POST
def device_delete(request, pk):
    device = get_object_or_404(Device, pk=pk)
    if device.logs.exists():
        messages.error(request, "This device has attendance records in the portal, so it cannot be removed. "
                                "Disable it instead (untick 'Enabled').")
        return redirect(device)
    name = device.name
    device.delete()  # removes the portal configuration only - nothing is sent to the device
    ActivityLog.log(request, "Device removed from portal", name)
    messages.success(request, f"{name} removed from the portal (the device itself was not touched).")
    return redirect("devices:list")


@staff_required
@require_POST
def simulate_punch(request, pk):
    device = get_object_or_404(Device, pk=pk, driver=Device.DRIVER_SIMULATOR)
    form = SimulatedPunchForm(request.POST)
    if form.is_valid():
        try:
            s = SimulatorSession(device).open()
            when = form.cleaned_data["when"]
            s.add_punch(form.cleaned_data["user_id"], timezone.localtime(when).replace(tzinfo=None) if when else None)
            messages.success(request, "Punch added to the simulator. Download attendance to import it.")
        except DeviceError as exc:
            messages.error(request, str(exc))
    else:
        messages.error(request, "Invalid punch.")
    return redirect(device)


@staff_required
def sync_center(request):
    devices = Device.objects.all()
    active = [d for d in devices if d.is_active]
    total_emp = Employee.objects.filter(status="active").count()
    rows = []
    for d in devices:
        linked_ids = set(d.user_links.values_list("employee_id", flat=True))
        targets = employees_for_device(d) if d.is_active else []
        rows.append({"device": d, "linked": len(linked_ids),
                     "missing": sum(1 for e in targets if e.pk not in linked_ids) if d.is_active else None})
    return render(request, "devices/sync_center.html", {
        "rows": rows,
        "active_count": len(active),
        "total_emp": total_emp,
        "actions": GLOBAL_ACTIONS,
        "jobs": DeviceJob.objects.select_related("device", "employee")[:15],
    })


@staff_required
@require_POST
def sync_action(request, action):
    if action not in GLOBAL_ACTIONS:
        messages.error(request, "Unknown action.")
        return redirect("devices:sync_center")
    job_type, label = GLOBAL_ACTIONS[action]
    payload = {"force": True} if action == "pull_attendance" and request.POST.get("force") else {}
    job = enqueue(job_type, payload=payload, user=request.user)
    ActivityLog.log(request, f"Sync: {label}")
    messages.success(request, f"{label} queued (job #{job.pk}).")
    return redirect(job)


@staff_required
def job_list(request):
    qs = DeviceJob.objects.select_related("device", "employee", "created_by")
    if request.GET.get("status"):
        qs = qs.filter(status=request.GET["status"])
    if request.GET.get("auto") == "0":
        qs = qs.filter(is_automatic=False)
    page = paginate(request, qs, 30)
    return render(request, "devices/job_list.html", {"page": page})


@staff_required
def job_detail(request, pk):
    job = get_object_or_404(DeviceJob.objects.select_related("device", "employee"), pk=pk)
    return render(request, "devices/job_detail.html", {"job": job})


@staff_required
def job_status(request, pk):
    job = get_object_or_404(DeviceJob, pk=pk)
    return JsonResponse({
        "status": job.status, "status_label": job.get_status_display(), "progress": job.progress,
        "log": job.log, "finished": job.is_finished, "state": job.result or {},
        "queue_position": DeviceJob.objects.filter(status__in=["pending", "running"], created_at__lt=job.created_at).count()
        if job.status == "pending" else 0,
    })


@staff_required
@require_POST
def job_cancel(request, pk):
    updated = DeviceJob.objects.filter(pk=pk, status="pending").update(
        status="cancelled", finished_at=timezone.now(), progress="Cancelled",
        result={"stage": "cancelled", "message": "Cancelled"})
    if not updated:  # a running enrollment stops at its next check (about 1 second)
        updated = DeviceJob.objects.filter(pk=pk, status="running", job_type=DeviceJob.TYPE_ENROLL).update(
            cancel_requested=True)
    if request.headers.get("Accept") == "application/json":
        return JsonResponse({"ok": bool(updated)})
    messages.info(request, "Cancelling..." if updated else "This job can no longer be cancelled.")
    return redirect("devices:job_detail", pk=pk)
