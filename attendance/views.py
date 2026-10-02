import calendar
import datetime as dt

from django.contrib import messages
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.crud import SimpleCrud
from core.decorators import safe_next, staff_required
from core.pagination import paginate
from core.excel import csv_response, xlsx_response
from core.models import ActivityLog
from core.timefmt import fmt_time
from devices.models import Device
from employees.models import Department, Designation, Employee
from employees.services import removed_names

from .engine import STATUS_LABELS, compute_attendance, daterange, fmt_minutes, summarize
from .forms import (HolidayForm, LeaveRequestForm, LeaveTypeForm, ManualPunchForm, ReviewForm, ShiftForm)
from .models import AttendanceLog, Holiday, LeaveRequest, LeaveType, RegularizationRequest, Shift


# ---------------------------------------------------------------- helpers
def parse_date(value, default):
    try:
        return dt.date.fromisoformat(value) if value else default
    except ValueError:
        return default


def parse_month(value):
    today = timezone.localdate()
    try:
        y, m = (int(x) for x in value.split("-")) if value else (today.year, today.month)
        return dt.date(y, m, 1)
    except (ValueError, AttributeError):
        return today.replace(day=1)


def month_end(first):
    return first.replace(day=calendar.monthrange(first.year, first.month)[1])


def report_employees(request):
    qs = Employee.objects.filter(status="active").select_related("department", "designation", "shift")
    if request.GET.get("department", "").isdigit():
        qs = qs.filter(department_id=request.GET["department"])
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(device_user_id__iexact=q))
    return list(qs.order_by("name"))


def fmt_t(value):
    return fmt_time(value)


# ---------------------------------------------------------------- punch logs
@staff_required
def log_list(request):
    today = timezone.localdate()
    date_from = parse_date(request.GET.get("from"), today - dt.timedelta(days=6))
    date_to = parse_date(request.GET.get("to"), today)
    tz = timezone.get_current_timezone()
    qs = AttendanceLog.objects.select_related("employee", "device", "employee__department").filter(
        punch_time__gte=timezone.make_aware(dt.datetime.combine(date_from, dt.time.min), tz),
        punch_time__lt=timezone.make_aware(dt.datetime.combine(date_to + dt.timedelta(days=1), dt.time.min), tz),
    )
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(employee__name__icontains=q) | Q(device_user_id__iexact=q))
    if request.GET.get("device", "").isdigit():
        qs = qs.filter(device_id=request.GET["device"])
    if request.GET.get("department", "").isdigit():
        qs = qs.filter(employee__department_id=request.GET["department"])
    if request.GET.get("source"):
        qs = qs.filter(source=request.GET["source"])
    if request.GET.get("unknown"):
        qs = qs.filter(employee__isnull=True)
    qs = qs.order_by("-punch_time")

    export = request.GET.get("export")
    if export in ("csv", "xlsx"):
        headers = ["Employee ID", "Name", "Department", "Date", "Time", "Device", "Verify", "State", "Source", "Note"]
        rows = []
        removed = removed_names()
        for log in qs.iterator(chunk_size=2000):
            local = timezone.localtime(log.punch_time)
            unknown = f"{removed[log.device_user_id]} (removed)" if log.device_user_id in removed else "(unknown)"
            rows.append([log.device_user_id, log.employee.name if log.employee else unknown,
                         log.employee.department.name if log.employee and log.employee.department else "",
                         local.date().isoformat(), fmt_time(local, seconds=True), log.device.name if log.device else "",
                         log.verify_label, log.punch_label, log.get_source_display(), log.note])
        name = f"punches_{date_from}_{date_to}"
        if export == "csv":
            return csv_response(name, headers, rows)
        return xlsx_response(name, "Punches", headers, rows, title=f"Punch log {date_from} to {date_to}",
                             col_widths={2: 26, 3: 18, 6: 14})
    page = paginate(request, qs, 100)
    return render(request, "attendance/log_list.html", {
        "page": page, "date_from": date_from, "date_to": date_to, "q": q, "removed": removed_names(),
        "devices": Device.objects.all(), "departments": Department.objects.all(),
        "sources": AttendanceLog.SOURCE_CHOICES,
    })


@staff_required
def manual_punch(request):
    initial = {}
    if request.GET.get("employee"):
        initial["employee"] = request.GET["employee"]
    if request.GET.get("at"):
        initial["punch_time"] = request.GET["at"]
    if request.GET.get("state") in ("0", "1"):
        initial["punch_state"] = int(request.GET["state"])
        initial["note"] = "Missed OUT punch" if request.GET["state"] == "1" else "Missed IN punch"
    next_url = request.POST.get("next") or request.GET.get("next") or ""
    form = ManualPunchForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        emp = form.cleaned_data["employee"]
        log = AttendanceLog.objects.create(
            employee=emp, device=None, device_user_id=emp.device_user_id,
            punch_time=form.cleaned_data["punch_time"], punch_state=form.cleaned_data["punch_state"],
            verify_type=-1, source=AttendanceLog.SOURCE_MANUAL, note=form.cleaned_data["note"], created_by=request.user,
        )
        ActivityLog.log(request, "Manual punch added", emp, f"{timezone.localtime(log.punch_time):%Y-%m-%d %H:%M} - {log.note}")
        messages.success(request, "Manual punch added.")
        return redirect(safe_next(request, next_url, reverse("attendance:logs")))
    return render(request, "attendance/manual_punch.html", {"form": form, "next": next_url})


@staff_required
@require_POST
def delete_manual_punch(request, pk):
    log = get_object_or_404(AttendanceLog, pk=pk)
    if log.source == AttendanceLog.SOURCE_DEVICE:
        messages.error(request, "Punches downloaded from a device cannot be deleted.")
    else:
        ActivityLog.log(request, "Manual punch deleted", log.employee or log.device_user_id,
                        f"{timezone.localtime(log.punch_time):%Y-%m-%d %H:%M} ({log.note})")
        log.delete()
        messages.success(request, "Manual punch deleted.")
    return redirect(safe_next(request, request.POST.get("next"), reverse("attendance:logs")))


# ---------------------------------------------------------------- attendance (main list)
ATTENDANCE_STATUSES = [
    ("present", "Present"), ("absent", "Absent"), ("late", "Late"), ("out_missing", "OUT missing"),
    ("in_office", "In office"), ("early", "Early leave"), ("half_day", "Half day"), ("leave", "Leave"),
    ("off", "Holiday / week off"), ("overtime", "Overtime"), ("short", "Short hours"),
]
PERIODS = [("today", "Today"), ("yesterday", "Yesterday"), ("this_week", "This week"), ("last_week", "Last week"),
           ("this_month", "This month"), ("last_month", "Last month")]
MAX_DAYS = 62


def _status_match(rec, status):
    return {
        "present": rec.has_punch, "absent": rec.status == "A", "late": rec.is_late, "out_missing": rec.out_missing,
        "in_office": rec.in_progress, "early": rec.is_early, "half_day": rec.status == "HD", "leave": rec.status == "L",
        "off": rec.status in ("H", "WO") or bool(rec.off_day), "overtime": bool(rec.overtime_minutes),
        "short": rec.short_hours,
    }.get(status, True)


def _period_range(period, today):
    if period == "yesterday":
        d = today - dt.timedelta(days=1)
        return d, d
    if period == "this_week":
        return today - dt.timedelta(days=today.weekday()), today
    if period == "last_week":
        start = today - dt.timedelta(days=today.weekday() + 7)
        return start, start + dt.timedelta(days=6)
    if period == "this_month":
        return today.replace(day=1), today
    if period == "last_month":
        last = today.replace(day=1) - dt.timedelta(days=1)
        return last.replace(day=1), last
    return today, today


@staff_required
def attendance_list(request):
    today = timezone.localdate()
    g = request.GET
    period = g.get("period", "")
    if g.get("from") or g.get("to"):
        period = "custom"
        date_from = parse_date(g.get("from"), today)
        date_to = parse_date(g.get("to"), date_from)
    else:
        period = period if period in dict(PERIODS) else "today"
        date_from, date_to = _period_range(period, today)
    if date_to < date_from:
        date_from, date_to = date_to, date_from
    if (date_to - date_from).days >= MAX_DAYS:
        date_from = date_to - dt.timedelta(days=MAX_DAYS - 1)
        messages.info(request, f"Showing the last {MAX_DAYS} days of the selected range.")

    employees = Employee.objects.filter(status="active").select_related("department", "designation", "shift")
    q = g.get("q", "").strip()
    if q:
        employees = employees.filter(Q(name__icontains=q) | Q(device_user_id__iexact=q))
    for field in ("department", "designation", "shift"):
        if g.get(field, "").isdigit():
            employees = employees.filter(**{f"{field}_id": g[field]})
    employees = list(employees.order_by("name"))
    data = compute_attendance(employees, date_from, date_to)

    rows = [(e, rec) for e in employees for rec in data[e.pk].values() if rec.status]
    counts = {key: sum(1 for _, r in rows if _status_match(r, key)) for key, _ in ATTENDANCE_STATUSES}
    status = g.get("status", "")
    if status in dict(ATTENDANCE_STATUSES):
        rows = [row for row in rows if _status_match(row[1], status)]

    sort = g.get("sort", "date")
    sorters = {
        "date": lambda x: (-x[1].date.toordinal(), x[0].name.lower()),
        "name": lambda x: (x[0].name.lower(), -x[1].date.toordinal()),
        "in": lambda x: (x[1].first_in is None, x[1].first_in or dt.datetime.max),
        "late": lambda x: (-x[1].late_minutes, x[0].name.lower()),
        "worked": lambda x: (-x[1].worked_minutes, x[0].name.lower()),
    }
    rows.sort(key=sorters.get(sort, sorters["date"]))

    export = g.get("export")
    if export in ("xlsx", "csv"):
        headers = ["Date", "Employee ID", "Name", "Department", "Shift", "Shift time", "IN", "OUT", "Worked",
                   "Late", "Early leave", "Overtime", "Status", "Remarks", "All punches"]
        out = []
        for e, r in rows:
            out_cell = fmt_t(r.last_out) or ("MISSING" if r.out_missing else ("In office" if r.in_progress else ""))
            remarks = ", ".join(x for x in [r.note, "Late" if r.is_late else "", "Short hours" if r.short_hours else "",
                                            r.holiday_name] if x)
            out.append([r.date, e.device_user_id, e.name, e.department.name if e.department_id else "", r.shift_name,
                        r.shift_window, fmt_t(r.first_in), out_cell, r.worked, fmt_minutes(r.late_minutes) if r.is_late else "",
                        fmt_minutes(r.early_minutes) if r.is_early else "", r.overtime, STATUS_LABELS.get(r.status, r.status),
                        remarks, ", ".join(fmt_time(p) for p in r.punches)])
        name = f"attendance_{date_from}_{date_to}"
        if export == "csv":
            return csv_response(name, headers, out)
        return xlsx_response(name, "Attendance", headers, out, title=f"Attendance {date_from} to {date_to}",
                             col_widths={1: 12, 3: 26, 4: 18, 6: 13, 14: 30, 15: 30})

    # chips describing active filters, each with a link that removes just that filter
    lookups = {
        "department": dict(Department.objects.values_list("pk", "name")),
        "designation": dict(Designation.objects.values_list("pk", "name")),
        "shift": dict(Shift.objects.values_list("pk", "name")),
    }
    chips = []

    def without(*keys):
        params = g.copy()
        for k in keys + ("page",):
            params.pop(k, None)
        return "?" + params.urlencode()

    if period == "custom":
        chips.append((f"{date_from:%d %b} – {date_to:%d %b %Y}", without("from", "to")))
    elif period != "today":
        chips.append((dict(PERIODS)[period], without("period")))
    if q:
        chips.append((f'Search: "{q}"', without("q")))
    for field in ("department", "designation", "shift"):
        if g.get(field, "").isdigit():
            chips.append((f"{field.capitalize()}: {lookups[field].get(int(g[field]), '?')}", without(field)))
    if status in dict(ATTENDANCE_STATUSES):
        chips.append((f"Status: {dict(ATTENDANCE_STATUSES)[status]}", without("status")))

    page = paginate(request, rows, 100)
    return render(request, "attendance/attendance_list.html", {
        "page": page, "date_from": date_from, "date_to": date_to, "period": period, "periods": PERIODS,
        "statuses": ATTENDANCE_STATUSES, "status": status, "counts": counts, "chips": chips, "q": q, "sort": sort,
        "departments": Department.objects.all(), "designations": Designation.objects.all(), "shifts": Shift.objects.all(),
        "total_rows": len(rows), "single_day": date_from == date_to,
    })


# ---------------------------------------------------------------- reports
@staff_required
def daily_report(request):
    day = parse_date(request.GET.get("date"), timezone.localdate())
    employees = report_employees(request)
    data = compute_attendance(employees, day, day)
    rows = [(e, data[e.pk][day]) for e in employees]
    status = request.GET.get("status", "")
    if status == "late":
        rows = [r for r in rows if r[1].is_late]
    elif status == "present":
        rows = [r for r in rows if r[1].has_punch]
    elif status == "out_missing":
        rows = [r for r in rows if r[1].out_missing]
    elif status:
        rows = [r for r in rows if r[1].status == status]
    summary = summarize(r[1] for r in rows)
    summary["off"] = summary.get("WO", 0) + summary.get("H", 0)
    if request.GET.get("export") == "xlsx":
        out = [(e.device_user_id, e.name, e.department.name if e.department else "", r.shift_name + (" (auto)" if r.shift_auto else ""),
                fmt_t(r.first_in), fmt_t(r.last_out), r.worked, fmt_minutes(r.late_minutes), fmt_minutes(r.early_minutes),
                r.overtime, r.label, len(r.punches)) for e, r in rows]
        return xlsx_response(f"daily_{day}", "Daily", ["Employee ID", "Name", "Department", "Shift", "In", "Out", "Worked",
                             "Late", "Early leave", "Overtime", "Status", "Punches"], out,
                             title=f"Daily attendance - {day:%d %b %Y}", col_widths={2: 26, 3: 18, 4: 22, 11: 18})
    return render(request, "attendance/daily_report.html", {
        "day": day, "rows": rows, "summary": summary, "departments": Department.objects.all(), "status": status,
        "prev_day": day - dt.timedelta(days=1), "next_day": day + dt.timedelta(days=1),
        "status_choices": [("present", "Present (any punch)"), ("A", "Absent"), ("late", "Late"), ("out_missing", "OUT punch missing"),
                           ("HD", "Half day"), ("L", "Leave")],
    })


@staff_required
def monthly_register(request):
    first = parse_month(request.GET.get("month"))
    last = month_end(first)
    employees = report_employees(request)
    data = compute_attendance(employees, first, last)
    days = list(daterange(first, last))
    rows = []
    for e in employees:
        recs = [data[e.pk][d] for d in days]
        rows.append((e, recs, summarize(recs)))
    holidays = set(Holiday.objects.filter(date__range=(first, last)).values_list("date", flat=True))
    if request.GET.get("export") == "xlsx":
        headers = ["Employee ID", "Name", "Department"] + [d.strftime("%d") for d in days] + [
            "Present", "Absent", "Half day", "OUT missing", "Leave", "Holiday", "Weekly off", "Late", "Worked (h:m)", "Overtime (h:m)"]
        out = []
        for e, recs, s in rows:
            out.append([e.device_user_id, e.name, e.department.name if e.department else ""] + [r.code for r in recs] + [
                s["present_days"], s.get("A", 0), s.get("HD", 0), s.get("out_missing", 0), s.get("L", 0), s.get("H", 0),
                s.get("WO", 0), s["late"], s["worked"], s["overtime"]])
        widths = {2: 26, 3: 16}
        widths.update({i: 5 for i in range(4, 4 + len(days))})
        return xlsx_response(f"register_{first:%Y_%m}", first.strftime("%b %Y"), headers, out,
                             title=f"Attendance register - {first:%B %Y}", col_widths=widths)
    prev_month = (first - dt.timedelta(days=1)).replace(day=1)
    next_month = last + dt.timedelta(days=1)
    return render(request, "attendance/monthly_register.html", {
        "first": first, "days": days, "rows": rows, "holidays": holidays,
        "departments": Department.objects.all(), "prev_month": prev_month, "next_month": next_month,
        "legend": STATUS_LABELS,
    })


@staff_required
def late_report(request):
    today = timezone.localdate()
    date_from = parse_date(request.GET.get("from"), today.replace(day=1))
    date_to = parse_date(request.GET.get("to"), today)
    if (date_to - date_from).days > 92:
        date_to = date_from + dt.timedelta(days=92)
        messages.info(request, "Range limited to 3 months.")
    employees = report_employees(request)
    data = compute_attendance(employees, date_from, date_to)
    kind = request.GET.get("kind", "late")
    rows = []
    for e in employees:
        for d, r in sorted(data[e.pk].items()):
            if (kind == "late" and r.is_late) or (kind == "early" and r.is_early) or (kind == "missed" and r.out_missing) \
                    or (kind == "absent" and r.status == "A") or (kind == "overtime" and r.overtime_minutes):
                rows.append((e, r))
    rows.sort(key=lambda x: (x[1].date, x[0].name), reverse=True)
    if request.GET.get("export") == "xlsx":
        out = [(r.date, e.device_user_id, e.name, e.department.name if e.department else "", fmt_t(r.first_in),
                fmt_t(r.last_out), fmt_minutes(r.late_minutes), fmt_minutes(r.early_minutes), r.overtime, r.label)
               for e, r in rows]
        return xlsx_response(f"{kind}_{date_from}_{date_to}", kind.capitalize(),
                             ["Date", "Employee ID", "Name", "Department", "In", "Out", "Late", "Early leave", "Overtime", "Status"],
                             out, title=f"{kind.capitalize()} report {date_from} to {date_to}", col_widths={1: 12, 3: 26, 4: 18})
    return render(request, "attendance/exception_report.html", {
        "rows": rows, "date_from": date_from, "date_to": date_to, "kind": kind,
        "departments": Department.objects.all(),
        "kinds": [("late", "Late arrivals"), ("early", "Early leaving"), ("missed", "OUT punch missing"),
                  ("absent", "Absences"), ("overtime", "Overtime")],
    })


@staff_required
def employee_card(request, pk):
    employee = get_object_or_404(Employee, pk=pk)
    first = parse_month(request.GET.get("month"))
    last = month_end(first)
    days = compute_attendance([employee], first, last)[employee.pk]
    recs = [days[d] for d in sorted(days)]
    return render(request, "attendance/employee_card.html", {
        "employee": employee, "first": first, "recs": recs, "summary": summarize(recs),
        "prev_month": (first - dt.timedelta(days=1)).replace(day=1), "next_month": last + dt.timedelta(days=1),
    })


# ---------------------------------------------------------------- leave & regularization (admin)
def leave_balances(employee, year=None):
    year = year or timezone.localdate().year
    out = []
    for lt in LeaveType.objects.all():
        used = 0
        for lr in employee.leaves.filter(leave_type=lt, status="approved", start_date__year=year):
            used += lr.days
        out.append({"type": lt, "used": used, "quota": lt.annual_quota,
                    "left": (lt.annual_quota - used) if lt.annual_quota else None})
    return out


@staff_required
def leave_list(request):
    status = request.GET.get("status", "pending")
    qs = LeaveRequest.objects.select_related("employee", "leave_type", "reviewed_by")
    if status != "all":
        qs = qs.filter(status=status)
    page = paginate(request, qs, 50)
    return render(request, "attendance/leave_list.html", {"page": page, "status": status,
                                                          "statuses": LeaveRequest.STATUS_CHOICES})


@staff_required
def leave_create(request):
    form = LeaveRequestForm(request.POST or None, initial={"employee": request.GET.get("employee")})
    if request.method == "POST" and form.is_valid():
        lr = form.save(commit=False)
        lr.status = LeaveRequest.STATUS_APPROVED
        lr.reviewed_by = request.user
        lr.reviewed_at = timezone.now()
        lr.review_note = "Added by administrator"
        lr.save()
        ActivityLog.log(request, "Leave added", lr.employee, f"{lr.leave_type} {lr.start_date} - {lr.end_date}")
        messages.success(request, "Leave recorded (approved).")
        return redirect("attendance:leaves")
    return render(request, "attendance/leave_form.html", {"form": form})


def _review(request, obj, on_approve=None):
    form = ReviewForm(request.POST)
    if not form.is_valid() or obj.status != "pending":
        messages.error(request, "This request was already handled." if obj.status != "pending" else "Invalid decision.")
        return False
    approve = form.cleaned_data["decision"] == "approve"
    obj.status = obj.STATUS_APPROVED if approve else obj.STATUS_REJECTED
    obj.reviewed_by = request.user
    obj.reviewed_at = timezone.now()
    obj.review_note = form.cleaned_data["note"]
    if approve and on_approve:
        on_approve(obj)
    obj.save()
    messages.success(request, f"Request {obj.get_status_display().lower()}.")
    return True


@staff_required
@require_POST
def leave_review(request, pk):
    lr = get_object_or_404(LeaveRequest, pk=pk)
    if _review(request, lr):
        ActivityLog.log(request, f"Leave {lr.status}", lr.employee, f"{lr.start_date} - {lr.end_date}")
    return redirect(safe_next(request, request.POST.get("next"), reverse("attendance:leaves")))


@staff_required
def regularization_list(request):
    status = request.GET.get("status", "pending")
    qs = RegularizationRequest.objects.select_related("employee", "reviewed_by")
    if status != "all":
        qs = qs.filter(status=status)
    page = paginate(request, qs, 50)
    return render(request, "attendance/regularization_list.html", {"page": page, "status": status,
                                                                   "statuses": RegularizationRequest.STATUS_CHOICES})


@staff_required
@require_POST
def regularization_review(request, pk):
    reg = get_object_or_404(RegularizationRequest, pk=pk)

    def create_log(r):
        when = timezone.make_aware(dt.datetime.combine(r.date, r.time))
        r.created_log = AttendanceLog.objects.create(
            employee=r.employee, device=None, device_user_id=r.employee.device_user_id, punch_time=when,
            punch_state=r.punch_state, verify_type=-1, source=AttendanceLog.SOURCE_REGULARIZATION,
            note=f"Regularization: {r.reason[:150]}", created_by=request.user,
        )

    if _review(request, reg, on_approve=create_log):
        ActivityLog.log(request, f"Regularization {reg.status}", reg.employee, f"{reg.date} {reg.time}")
    return redirect("attendance:regularizations")


shift_crud = SimpleCrud(
    Shift, ShiftForm, "shift", "Shifts",
    [("Name", "name"), ("Start", lambda o: fmt_time(o.start_time)), ("End", lambda o: fmt_time(o.end_time)),
     ("Grace in/out", lambda o: f"{o.grace_in_minutes} / {o.grace_out_minutes} min"),
     ("Full / half day", lambda o: f"{fmt_minutes(o.full_day_minutes) or '-'} / {fmt_minutes(o.half_day_minutes) or '-'} h"),
     ("Weekly off", "weekly_off_labels"),
     ("Auto-detect", lambda o: (fmt_time(dt.datetime.combine(dt.date.today(), o.start_time) - dt.timedelta(minutes=o.detect_before_minutes))
                                + "–" + fmt_time(dt.datetime.combine(dt.date.today(), o.start_time) + dt.timedelta(minutes=o.detect_after_minutes)))
      if o.auto_detect else "No"),
     ("Fixed employees", lambda o: o.employees.count())],
    help_text="Employees with a fixed shift always use it. Employees without one get, each day, the auto-detect "
              "shift whose window (Auto-detect column) contains their first punch – or the default shift when "
              "auto-detection is off in Settings. Weekly offs: 0=Mon ... 6=Sun.",
)
holiday_crud = SimpleCrud(Holiday, HolidayForm, "holiday", "Holidays",
                          [("Date", lambda o: o.date.strftime("%a, %d %b %Y")), ("Name", "name")],
                          ordering=["-date"])
leavetype_crud = SimpleCrud(LeaveType, LeaveTypeForm, "leavetype", "Leave types",
                            [("Name", "name"), ("Code", "code"), ("Days / year", lambda o: o.annual_quota or "Unlimited"),
                             ("Paid", lambda o: "Yes" if o.is_paid else "No")])
