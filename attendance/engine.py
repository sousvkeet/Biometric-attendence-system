"""
Attendance calculation.

Daily attendance is computed on demand from the raw punches, so changes to
shifts, holidays, leave approvals or manual punches are always reflected
immediately in every report.
"""
import datetime as dt
from collections import defaultdict
from dataclasses import dataclass, field

from django.db.models import Min
from django.utils import timezone

from core.models import SystemSetting
from core.timefmt import fmt_time

from .models import AttendanceLog, Holiday, LeaveRequest, Shift

STATUS_LABELS = {
    "P": "Present",
    "A": "Absent",
    "HD": "Half day",
    "L": "Leave",
    "H": "Holiday",
    "WO": "Weekly off",
    "": "-",
}
STATUS_CSS = {
    "P": "success", "A": "danger", "HD": "warning",
    "L": "info", "H": "secondary", "WO": "secondary", "": "light",
}

FALLBACK_SHIFT = Shift(name="General", start_time=dt.time(9, 0), end_time=dt.time(18, 0))


def fmt_minutes(minutes):
    if not minutes:
        return ""
    return f"{minutes // 60}:{minutes % 60:02d}"


@dataclass
class DayRecord:
    date: dt.date
    status: str = ""
    punches: list = field(default_factory=list)
    first_in: dt.datetime | None = None
    last_out: dt.datetime | None = None
    worked_minutes: int = 0
    late_minutes: int = 0
    early_minutes: int = 0
    overtime_minutes: int = 0
    is_late: bool = False
    is_early: bool = False
    off_day: str = ""
    holiday_name: str = ""
    leave_code: str = ""
    in_progress: bool = False   # one punch, shift not over yet ("in office")
    out_missing: bool = False   # one punch and the shift is over
    short_hours: bool = False   # in + out, but worked less than the shift's full-day minutes
    shift_name: str = ""
    shift_auto: bool = False    # shift was auto-detected from the first punch
    expected_in: dt.datetime | None = None
    expected_out: dt.datetime | None = None
    note: str = ""

    @property
    def label(self):
        text = STATUS_LABELS.get(self.status, self.status)
        if self.off_day and self.status == "P":
            text += f" ({STATUS_LABELS[self.off_day]})"
        if self.in_progress:
            text += " · in office"
        elif self.out_missing:
            text += " · OUT missing"
        return text

    @property
    def shift_window(self):
        if not self.expected_in:
            return ""
        return f"{fmt_time(self.expected_in)}–{fmt_time(self.expected_out)}"

    @property
    def code(self):
        if self.status == "L" and self.leave_code:
            return self.leave_code
        return self.status

    @property
    def css(self):
        return STATUS_CSS.get(self.status, "light")

    @property
    def worked(self):
        return fmt_minutes(self.worked_minutes)

    @property
    def overtime(self):
        return fmt_minutes(self.overtime_minutes)

    @property
    def has_punch(self):
        return bool(self.punches)


def daterange(start, end):
    d = start
    while d <= end:
        yield d
        d += dt.timedelta(days=1)


def _local_naive(ts):
    return timezone.localtime(ts).replace(tzinfo=None)


def compute_attendance(employees, start, end, today=None, now=None):
    """Return {employee_id: {date: DayRecord}} for every employee and every date in [start, end].

    Rules: the first punch of the work day is IN (the day is Present), the last punch is OUT.
    With a single punch the day is "in office" until the shift ends, then "OUT missing".
    """
    employees = list(employees)
    today = today or timezone.localdate()
    now = now or timezone.localtime().replace(tzinfo=None)
    if not employees:
        return {}
    settings_obj = SystemSetting.get()
    default_shift = settings_obj.default_shift or Shift.objects.order_by("pk").first() or FALLBACK_SHIFT
    auto_shifts = list(Shift.objects.filter(auto_detect=True)) if settings_obj.auto_shift_detection else []
    # Fixed shift if one is assigned; otherwise auto-detected per day (when enabled), else the default shift.
    auto_ids = {e.pk for e in employees if not e.shift_id and auto_shifts}
    shifts = {e.pk: (e.shift if e.shift_id else default_shift) for e in employees}
    ids = [e.pk for e in employees]

    holidays = dict(Holiday.objects.filter(date__range=(start, end)).values_list("date", "name"))
    leaves = defaultdict(dict)
    for lr in LeaveRequest.objects.filter(
        employee_id__in=ids, status=LeaveRequest.STATUS_APPROVED, start_date__lte=end, end_date__gte=start
    ).select_related("leave_type"):
        for d in lr.dates():
            if start <= d <= end:
                leaves[lr.employee_id][d] = lr.leave_type.code

    first_seen = dict(
        AttendanceLog.objects.filter(employee_id__in=ids).values("employee_id")
        .annotate(first=Min("punch_time")).values_list("employee_id", "first")
    )

    tz = timezone.get_current_timezone()
    range_start = timezone.make_aware(dt.datetime.combine(start - dt.timedelta(days=2), dt.time.min), tz)
    range_end = timezone.make_aware(dt.datetime.combine(end + dt.timedelta(days=2), dt.time.min), tz)
    raw = defaultdict(list)
    for emp_id, ts in AttendanceLog.objects.filter(
        employee_id__in=ids, punch_time__gte=range_start, punch_time__lt=range_end
    ).values_list("employee_id", "punch_time"):
        raw[emp_id].append(_local_naive(ts))

    punches = defaultdict(dict)       # emp -> work date -> [punches]
    day_shift = defaultdict(dict)     # emp -> work date -> detected shift (auto employees only)
    for emp_id, times in raw.items():
        times = sorted(set(times))
        if emp_id in auto_ids:
            for wd, (shift, group) in detect_shift_sessions(times, auto_shifts).items():
                if start <= wd <= end:
                    punches[emp_id][wd] = group
                    day_shift[emp_id][wd] = shift
        else:
            for local in times:
                wd = shifts[emp_id].work_date(local)
                if start <= wd <= end:
                    punches[emp_id].setdefault(wd, []).append(local)

    result = {}
    for emp in employees:
        first = first_seen.get(emp.pk)
        # Attendance is counted from the join date, else the first punch; employees created in the portal
        # count from their creation date. Device-imported people who never punched are not counted.
        if emp.join_date:
            first_date = emp.join_date
        elif first:
            first_date = timezone.localtime(first).date()
        elif not emp.source_device_id:
            first_date = timezone.localtime(emp.created_at).date()
        else:
            first_date = dt.date.max
        days = {}
        for d in daterange(start, end):
            detected = day_shift[emp.pk].get(d)
            shift = detected or shifts[emp.pk]
            rec = _compute_day(
                d, shift, shift.weekly_off_set, punches[emp.pk].get(d, []), holidays.get(d, ""),
                leaves[emp.pk].get(d, ""), today, first_date, now,
            )
            rec.shift_auto = detected is not None
            days[d] = rec
        result[emp.pk] = days
    return result


AUTO_SESSION_AFTER_END = dt.timedelta(hours=4)  # punches up to 4 h after the detected shift ends belong to it


def match_shift(punch, shifts):
    """Pick (shift, work_date) for a first punch: a shift whose detection window contains the punch wins
    (closest start if several); otherwise simply the closest shift start."""
    best = None
    for shift in shifts:
        for wd in (punch.date() - dt.timedelta(days=1), punch.date(), punch.date() + dt.timedelta(days=1)):
            delta = (punch - dt.datetime.combine(wd, shift.start_time)).total_seconds() / 60
            inside = -shift.detect_before_minutes <= delta <= shift.detect_after_minutes
            key = (0 if inside else 1, abs(delta), shift.pk or 0)
            if best is None or key < best[0]:
                best = (key, shift, wd)
    return best[1], best[2]


def detect_shift_sessions(times, shifts):
    """Group sorted punches into duty sessions: {work_date: (shift, [punches])}.

    The first punch of a session decides the shift (see match_shift); later punches up to 4 hours after
    that shift's end belong to the same work day (so night shifts crossing midnight stay one day).
    """
    sessions = {}
    i = 0
    while i < len(times):
        shift, wd = match_shift(times[i], shifts)
        _, shift_end = shift.expected_window(wd)
        limit = shift_end + AUTO_SESSION_AFTER_END
        group = [times[i]]
        i += 1
        while i < len(times) and times[i] <= limit:
            group.append(times[i])
            i += 1
        if wd in sessions:  # a second session on the same work day: keep the first shift, add the punches
            sessions[wd][1].extend(group)
        else:
            sessions[wd] = (shift, group)
    return sessions


def _compute_day(d, shift, offs, punches, holiday, leave_code, today, first_date, now):
    rec = DayRecord(date=d, punches=punches, holiday_name=holiday, leave_code=leave_code, shift_name=shift.name)
    off = "H" if holiday else ("WO" if d.weekday() in offs else "")
    rec.expected_in, rec.expected_out = shift.expected_window(d)

    if punches:
        rec.status = "P"  # first punch = IN = present
        rec.first_in = punches[0]
        if len(punches) > 1 and (punches[-1] - punches[0]) >= dt.timedelta(minutes=1):
            rec.last_out = punches[-1]  # last punch = OUT
            rec.worked_minutes = int((rec.last_out - rec.first_in).total_seconds() // 60)
        elif now < rec.expected_out:
            rec.in_progress = True
        else:
            rec.out_missing, rec.note = True, f"OUT punch missing (shift ends {fmt_time(rec.expected_out)})"
        if off:
            rec.off_day = off
            rec.overtime_minutes = rec.worked_minutes
            return rec
        late = int((rec.first_in - rec.expected_in).total_seconds() // 60)
        if late > shift.grace_in_minutes:
            rec.is_late, rec.late_minutes = True, late
        if rec.last_out is None:
            return rec
        early = int((rec.expected_out - rec.last_out).total_seconds() // 60)
        if early > shift.grace_out_minutes:
            rec.is_early, rec.early_minutes = True, early
        if shift.half_day_minutes and rec.worked_minutes < shift.half_day_minutes:
            rec.status, rec.note = "HD", f"Worked less than {fmt_minutes(shift.half_day_minutes)} h"
        elif shift.full_day_minutes and rec.worked_minutes < shift.full_day_minutes:
            rec.short_hours = True
        extra = rec.worked_minutes - shift.duration_minutes
        if extra >= shift.overtime_after_minutes and extra > 0:
            rec.overtime_minutes = extra
        return rec

    if off:
        rec.status = off
    elif leave_code:
        rec.status = "L"
    elif d > today or d < first_date:
        rec.status = ""
    else:
        rec.status = "A"
    return rec


def summarize(days):
    """Totals for an iterable of DayRecord."""
    s = defaultdict(int)
    for r in days:
        s[r.status or "NA"] += 1
        s["late"] += r.is_late
        s["early"] += r.is_early
        s["out_missing"] += r.out_missing
        s["in_office"] += r.in_progress
        s["worked_minutes"] += r.worked_minutes
        s["overtime_minutes"] += r.overtime_minutes
    s["present_days"] = s["P"] + s["HD"] * 0.5
    s["worked"] = fmt_minutes(s["worked_minutes"]) or "0:00"
    s["overtime"] = fmt_minutes(s["overtime_minutes"]) or "0:00"
    return dict(s)
