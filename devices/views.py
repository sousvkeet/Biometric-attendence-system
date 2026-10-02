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
from .sync import employees_for_device

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
    return render(request, "devices/list.html", {"devices": devices})


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
    })


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
