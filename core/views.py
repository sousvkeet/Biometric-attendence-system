import datetime as dt
from collections import Counter

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone

from attendance.engine import compute_attendance
from attendance.models import AttendanceLog, LeaveRequest, RegularizationRequest
from devices.models import Device, DeviceJob
from employees.models import Employee, RemovedDeviceUser
from employees.services import removed_names

from .decorators import staff_required
from .pagination import paginate
from .forms import SystemSettingForm
from .models import ActivityLog, SystemSetting
from .timefmt import fmt_time


@login_required
def home(request):
    if request.user.is_staff:
        return redirect("dashboard")
    if hasattr(request.user, "employee"):
        return redirect("selfservice:home")
    messages.error(request, "Your account has no access. Contact the administrator.")
    return redirect("logout_page")


def logout_page(request):
    """POST only, so other websites cannot sign users out."""
    if request.method != "POST":
        return redirect("home")
    from django.contrib.auth import logout
    logout(request)
    return redirect("login")


def _today_counts(employees, data, today):
    counts = Counter()
    for e in employees:
        rec = data[e.pk][today]
        key = "present" if rec.has_punch else {"A": "absent", "L": "leave", "H": "off", "WO": "off"}.get(rec.status, "absent")
        counts[key] += 1
        if rec.is_late:
            counts["late"] += 1
    counts["total"] = len(employees)
    return counts


def _live_status(device):
    live = getattr(device, "live", None)
    if not device.is_active or not device.live_enabled:
        return "off"
    if live is None or not live.is_fresh:
        return "waiting"
    return live.status


@staff_required
def dashboard(request):
    today = timezone.localdate()
    employees = list(Employee.objects.filter(status="active").select_related("shift", "department"))
    week_start = today - dt.timedelta(days=6)
    data = compute_attendance(employees, week_start, today, today=today)

    today_counts = _today_counts(employees, data, today)
    dept_rows = {}
    late_list = []
    for e in employees:
        rec = data[e.pk][today]
        key = "present" if rec.has_punch else {"A": "absent", "L": "leave", "H": "off", "WO": "off"}.get(rec.status, "absent")
        if rec.is_late:
            late_list.append((e, rec))
        dname = e.department.name if e.department_id else "No department"
        row = dept_rows.setdefault(dname, Counter())
        row["total"] += 1
        row[key] += 1

    trend = []
    for i in range(7):
        d = week_start + dt.timedelta(days=i)
        present = sum(1 for e in employees if data[e.pk][d].has_punch)
        late = sum(1 for e in employees if data[e.pk][d].is_late)
        trend.append({"label": d.strftime("%a %d"), "present": present, "late": late})

    late_list.sort(key=lambda x: -x[1].late_minutes)
    devices = list(Device.objects.select_related("live"))
    for d in devices:
        d.live_state = _live_status(d)
    context = {
        "today": today,
        "total_employees": len(employees),
        "counts": today_counts,
        "dept_rows": sorted(dept_rows.items()),
        "trend": trend,
        "late_list": late_list[:8],
        "devices": devices,
        "pending_leaves": LeaveRequest.objects.filter(status="pending").select_related("employee", "leave_type")[:5],
        "pending_regs": RegularizationRequest.objects.filter(status="pending").count(),
        "recent_jobs": DeviceJob.objects.select_related("device", "employee")[:6],
        "unknown_punch_ids": AttendanceLog.objects.filter(employee__isnull=True).exclude(
            device_user_id__in=RemovedDeviceUser.objects.values("device_user_id")).values("device_user_id").distinct().count(),
    }
    return render(request, "core/dashboard.html", context)


@staff_required
def live_monitor(request):
    return render(request, "core/live_monitor.html", {"today": timezone.localdate()})


def _initials(name):
    parts = [p for p in (name or "?").replace(".", " ").split() if p]
    return ((parts[0][0] + (parts[-1][0] if len(parts) > 1 else "")) if parts else "?").upper()


@staff_required
def live_feed(request):
    """New punches since `after` (an AttendanceLog id) + today's counters + device live status."""
    today = timezone.localdate()
    day_start = timezone.make_aware(dt.datetime.combine(today, dt.time.min))
    todays = AttendanceLog.objects.filter(punch_time__gte=day_start).select_related(
        "employee", "employee__department", "employee__shift", "device")
    try:
        after = max(int(request.GET.get("after") or 0), 0)
    except ValueError:
        after = 0
    raw_limit = request.GET.get("limit", "")
    limit = min(max(int(raw_limit), 1), 50) if raw_limit.isdigit() else 15
    newest_id = AttendanceLog.objects.order_by("-pk").values_list("pk", flat=True).first() or 0
    if after:
        rows = list(todays.filter(pk__gt=after).order_by("pk")[:50])
        cursor = rows[-1].pk if len(rows) == 50 else newest_id
    else:
        rows = list(todays.order_by("-punch_time", "-pk")[:limit])[::-1]
        cursor = newest_id

    employees = list(Employee.objects.filter(status="active").select_related("shift"))
    involved = {r.employee for r in rows if r.employee_id}
    data = compute_attendance(employees + [e for e in involved if not e.is_active], today, today, today=today)
    removed = removed_names() if any(not r.employee_id for r in rows) else {}
    punches = []
    for r in rows:
        local = timezone.localtime(r.punch_time)
        item = {
            "id": r.pk, "time": fmt_time(local, seconds=True), "ts": local.isoformat(),
            "user_id": r.device_user_id, "device": r.device.name if r.device_id else r.get_source_display(),
            "verify": r.verify_label, "kind": "", "late": False, "late_minutes": 0,
            "name": f"Unknown ID {r.device_user_id}", "initials": "?", "department": "", "url": "",
        }
        if not r.employee_id and r.device_user_id in removed:
            item.update(name=f"{removed[r.device_user_id]} (removed)", initials=_initials(removed[r.device_user_id]))
        if r.employee_id:
            e = r.employee
            item.update(name=e.name, initials=_initials(e.name), url=e.get_absolute_url(),
                        department=e.department.name if e.department_id else "")
            rec = data.get(e.pk, {}).get(today)
            if rec is not None and rec.first_in is not None:
                is_in = rec.first_in == local.replace(tzinfo=None)
                item["kind"] = "in" if is_in else "out"
                if is_in and rec.is_late:
                    item.update(late=True, late_minutes=rec.late_minutes)
        punches.append(item)

    devices = []
    for d in Device.objects.select_related("live"):
        live = getattr(d, "live", None)
        devices.append({"name": d.name, "status": _live_status(d),
                        "last_event": fmt_time(live.last_event_at, seconds=True)
                        if live and live.last_event_at else ""})
    return JsonResponse({
        "cursor": cursor, "punches": punches,
        "counts": _today_counts(employees, data, today),
        "devices": devices, "server_time": fmt_time(timezone.localtime(), seconds=True),
    })


@staff_required
def settings_view(request):
    obj = SystemSetting.get()
    form = SystemSettingForm(request.POST or None, instance=obj)
    if request.method == "POST" and form.is_valid():
        form.save()
        ActivityLog.log(request, "Settings updated")
        messages.success(request, "Settings saved.")
        return redirect("settings")
    return render(request, "core/settings.html", {"form": form, "obj": obj})


@staff_required
def activity_log(request):
    qs = ActivityLog.objects.select_related("user")
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(action__icontains=q) | qs.filter(target__icontains=q)
    page = paginate(request, qs, 50)
    return render(request, "core/activity_log.html", {"page": page, "q": q})


def healthz(request):
    """Monitoring endpoint (no login, no personal data): 200 when the web app and database work.
    `worker` reports whether the background device worker has sent a heartbeat in the last minute."""
    from django.db import connection
    try:
        with connection.cursor() as cur:
            cur.execute("SELECT 1")
        worker = SystemSetting.get().worker_online
    except Exception:
        import logging
        logging.getLogger("core").exception("Health check: database not reachable")
        resp = JsonResponse({"status": "error", "database": False}, status=503)
        resp._has_been_logged = True  # logged once above with the real cause
        return resp
    return JsonResponse({"status": "ok", "database": True, "worker": worker})


def first_run(request):
    """First-time setup page: create the main administrator (only while none exists)."""
    from django.contrib.auth import get_user_model, login
    from django.db import transaction

    from .firstrun import code_required, create_defaults, delete_setup_code, setup_needed
    from .forms import FirstRunForm
    if not setup_needed():
        return redirect("home")
    need_code = code_required(request)
    if need_code:
        from .firstrun import get_setup_code
        get_setup_code()  # make sure a code exists (and is logged) before the form is shown
    form = FirstRunForm(request.POST or None, code_required=need_code)
    if request.method == "POST" and form.is_valid():
        d = form.cleaned_data
        with transaction.atomic():
            if not setup_needed():  # someone else finished setup a moment ago
                return redirect("login")
            create_defaults(company=d["company_name"])
            user = get_user_model().objects.create_superuser(d["username"], d["email"], d["password1"])
        delete_setup_code()
        login(request, user, backend="django.contrib.auth.backends.ModelBackend")
        ActivityLog.log(request, "First-time setup completed", user.username, f"email {user.email}")
        messages.success(request, f"Welcome, {user.username}! Next: add your devices (Devices → Add device), "
                                  "set your shifts and configure backups.")
        return redirect("dashboard")
    return render(request, "registration/first_run.html", {"form": form, "need_code": need_code})
