import datetime as dt

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

WEEKDAY_CHOICES = [(0, "Mon"), (1, "Tue"), (2, "Wed"), (3, "Thu"), (4, "Fri"), (5, "Sat"), (6, "Sun")]

VERIFY_TYPES = {0: "Password", 1: "Fingerprint", 2: "Card", 3: "Password", 4: "Card", 15: "Face", 25: "Palm"}
PUNCH_STATES = {0: "Check-in", 1: "Check-out", 2: "Break-out", 3: "Break-in", 4: "OT-in", 5: "OT-out", 255: "Auto"}


class Shift(models.Model):
    name = models.CharField(max_length=60, unique=True)
    start_time = models.TimeField(default=dt.time(9, 0))
    end_time = models.TimeField(default=dt.time(18, 0), help_text="May be earlier than start for night shifts.")
    grace_in_minutes = models.PositiveIntegerField("Late grace (min)", default=15)
    grace_out_minutes = models.PositiveIntegerField("Early-leave grace (min)", default=15)
    full_day_minutes = models.PositiveIntegerField(
        "Minimum minutes for full day", default=420,
        help_text="Days with fewer worked minutes are flagged 'short hours' (still Present). 0 = off.",
    )
    half_day_minutes = models.PositiveIntegerField(
        "Half-day below (minutes)", default=240,
        help_text="Days with IN and OUT but fewer worked minutes are marked Half day. 0 = never.",
    )
    overtime_after_minutes = models.PositiveIntegerField(
        "Overtime threshold (min)", default=30, help_text="Extra minutes after shift length before overtime counts.",
    )
    weekly_offs = models.CharField(
        max_length=20, default="6", blank=True, help_text="Comma separated weekday numbers (0=Mon … 6=Sun).",
    )
    auto_detect = models.BooleanField(
        "Use for auto-detection", default=True,
        help_text="Employees without a fixed shift get this shift on days their first punch falls in its window.",
    )
    detect_before_minutes = models.PositiveIntegerField(
        "Detect: minutes before start", default=120, help_text="First punch this early before the start still matches.",
    )
    detect_after_minutes = models.PositiveIntegerField(
        "Detect: minutes after start", default=180, help_text="First punch this late after the start still matches.",
    )

    class Meta:
        ordering = ["start_time", "name"]

    def __str__(self):
        from core.timefmt import fmt_time
        return f"{self.name} ({fmt_time(self.start_time)}–{fmt_time(self.end_time)})"

    def clean(self):
        if self.half_day_minutes and self.full_day_minutes and self.half_day_minutes > self.full_day_minutes:
            raise ValidationError("Half-day minutes cannot be more than full-day minutes.")
        try:
            self.weekly_off_set
        except ValueError:
            raise ValidationError({"weekly_offs": "Use numbers 0-6 separated by commas."})

    @property
    def weekly_off_set(self):
        out = set()
        for part in (self.weekly_offs or "").split(","):
            part = part.strip()
            if part:
                n = int(part)
                if not 0 <= n <= 6:
                    raise ValueError(n)
                out.add(n)
        return out

    @property
    def weekly_off_labels(self):
        names = dict(WEEKDAY_CHOICES)
        return ", ".join(names[d] for d in sorted(self.weekly_off_set)) or "None"

    @property
    def is_overnight(self):
        return self.end_time <= self.start_time

    @property
    def duration_minutes(self):
        start = dt.datetime.combine(dt.date.today(), self.start_time)
        end = dt.datetime.combine(dt.date.today(), self.end_time)
        if end <= start:
            end += dt.timedelta(days=1)
        return int((end - start).total_seconds() // 60)

    @property
    def day_boundary_offset(self):
        """Punches are assigned to a work date whose day starts 4 hours before shift start."""
        return dt.timedelta(hours=self.start_time.hour, minutes=self.start_time.minute) - dt.timedelta(hours=4)

    def work_date(self, local_naive):
        return (local_naive - self.day_boundary_offset).date()

    def expected_window(self, work_date):
        start = dt.datetime.combine(work_date, self.start_time)
        end = dt.datetime.combine(work_date, self.end_time)
        if end <= start:
            end += dt.timedelta(days=1)
        return start, end


class Holiday(models.Model):
    date = models.DateField(unique=True)
    name = models.CharField(max_length=100)

    class Meta:
        ordering = ["date"]

    def __str__(self):
        return f"{self.date} {self.name}"


class AttendanceLog(models.Model):
    """A single punch. Device punches are immutable copies of the device log."""

    SOURCE_DEVICE = "device"
    SOURCE_MANUAL = "manual"
    SOURCE_REGULARIZATION = "regularization"
    SOURCE_CHOICES = [
        (SOURCE_DEVICE, "Device"),
        (SOURCE_MANUAL, "Manual entry"),
        (SOURCE_REGULARIZATION, "Regularization"),
    ]

    employee = models.ForeignKey(
        "employees.Employee", null=True, blank=True, on_delete=models.SET_NULL, related_name="logs"
    )
    device = models.ForeignKey(
        "devices.Device", null=True, blank=True, on_delete=models.PROTECT, related_name="logs"
    )
    device_user_id = models.CharField(max_length=24, db_index=True)
    punch_time = models.DateTimeField(db_index=True)
    verify_type = models.SmallIntegerField(default=1)
    punch_state = models.SmallIntegerField(default=0)
    source = models.CharField(max_length=20, choices=SOURCE_CHOICES, default=SOURCE_DEVICE)
    note = models.CharField(max_length=200, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-punch_time"]
        constraints = [
            models.UniqueConstraint(fields=["device", "device_user_id", "punch_time"], name="uniq_device_punch"),
        ]
        indexes = [models.Index(fields=["employee", "punch_time"])]

    def __str__(self):
        return f"{self.device_user_id} {self.punch_time}"

    @property
    def verify_label(self):
        if self.source != self.SOURCE_DEVICE:
            return self.get_source_display()
        return VERIFY_TYPES.get(self.verify_type, f"Type {self.verify_type}")

    @property
    def punch_label(self):
        return PUNCH_STATES.get(self.punch_state, f"State {self.punch_state}")


class LeaveType(models.Model):
    name = models.CharField(max_length=60, unique=True)
    code = models.CharField(max_length=10, unique=True)
    annual_quota = models.PositiveIntegerField(default=0, help_text="Days per year (0 = unlimited).")
    is_paid = models.BooleanField(default=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class ReviewMixin(models.Model):
    STATUS_PENDING = "pending"
    STATUS_APPROVED = "approved"
    STATUS_REJECTED = "rejected"
    STATUS_CANCELLED = "cancelled"
    STATUS_CHOICES = [
        (STATUS_PENDING, "Pending"),
        (STATUS_APPROVED, "Approved"),
        (STATUS_REJECTED, "Rejected"),
        (STATUS_CANCELLED, "Cancelled"),
    ]
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_PENDING, db_index=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        abstract = True


class LeaveRequest(ReviewMixin):
    employee = models.ForeignKey("employees.Employee", on_delete=models.CASCADE, related_name="leaves")
    leave_type = models.ForeignKey(LeaveType, on_delete=models.PROTECT)
    start_date = models.DateField()
    end_date = models.DateField()
    reason = models.TextField(blank=True)

    class Meta:
        ordering = ["-start_date"]

    def __str__(self):
        return f"{self.employee} {self.leave_type} {self.start_date}"

    def clean(self):
        if self.start_date and self.end_date and self.end_date < self.start_date:
            raise ValidationError({"end_date": "End date must be on or after start date."})

    @property
    def days(self):
        return (self.end_date - self.start_date).days + 1

    def dates(self):
        d = self.start_date
        while d <= self.end_date:
            yield d
            d += dt.timedelta(days=1)


class RegularizationRequest(ReviewMixin):
    """Request to add a missed punch (approved requests become AttendanceLog rows)."""

    employee = models.ForeignKey("employees.Employee", on_delete=models.CASCADE, related_name="regularizations")
    date = models.DateField()
    time = models.TimeField()
    punch_state = models.SmallIntegerField(choices=[(0, "Check-in"), (1, "Check-out")], default=0)
    reason = models.TextField()
    created_log = models.ForeignKey(AttendanceLog, null=True, blank=True, on_delete=models.SET_NULL, related_name="+")

    class Meta:
        ordering = ["-date", "-created_at"]

    def __str__(self):
        return f"{self.employee} {self.date} {self.time}"
