from django.conf import settings
from django.db import models
from django.utils import timezone


class SystemSetting(models.Model):
    """Singleton row (pk=1) holding portal-wide settings."""

    company_name = models.CharField(max_length=120, default="My Company")
    TIME_FORMAT_CHOICES = [("24", "24-hour (e.g. 18:05)"), ("12", "12-hour AM/PM (e.g. 6:05 PM)")]
    time_format = models.CharField("Time format", max_length=2, choices=TIME_FORMAT_CHOICES, default="24",
                                   help_text="How times are shown on every page, report and export.")

    auto_attendance_sync = models.BooleanField(
        "Auto-download attendance", default=True,
        help_text="Periodically download new punches from every active device.",
    )
    attendance_sync_interval = models.PositiveIntegerField("Attendance sync interval (minutes)", default=5)

    auto_user_sync = models.BooleanField(
        "Auto-sync users & fingerprints", default=True,
        help_text="Periodically collect users/fingerprints from enroll devices and copy them to all other devices.",
    )
    user_sync_interval = models.PositiveIntegerField("User sync interval (minutes)", default=30)

    push_on_employee_save = models.BooleanField(
        "Push employee to devices on save", default=True,
        help_text="When an employee is created or edited in the portal, send the change to the devices immediately.",
    )
    device_timeout = models.PositiveIntegerField("Device timeout (seconds)", default=30)
    auto_shift_detection = models.BooleanField(
        "Auto-detect shift from punches", default=True,
        help_text="Employees without a fixed shift: each day's shift is chosen from their first punch "
                  "(among shifts marked 'use for auto-detection'). Off = they use the default shift.",
    )
    default_shift = models.ForeignKey(
        "attendance.Shift", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        help_text="Used for employees without a shift.",
    )

    # Written by the background worker only (via queryset.update so web saves never clobber them).
    worker_heartbeat = models.DateTimeField(null=True, blank=True, editable=False)
    last_auto_attendance_sync = models.DateTimeField(null=True, blank=True, editable=False)
    last_auto_user_sync = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        verbose_name = "System setting"

    def __str__(self):
        return "System settings"

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        from .timefmt import reset_cache
        reset_cache()

    @property
    def worker_online(self):
        return bool(self.worker_heartbeat and (timezone.now() - self.worker_heartbeat).total_seconds() < 60)


class ActivityLog(models.Model):
    """Audit trail of administrative actions."""

    user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    action = models.CharField(max_length=80)
    target = models.CharField(max_length=200, blank=True)
    details = models.TextField(blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"{self.action} {self.target}"

    @classmethod
    def log(cls, request_or_user, action, target="", details=""):
        user, ip = None, None
        if hasattr(request_or_user, "user"):
            user = request_or_user.user if request_or_user.user.is_authenticated else None
            ip = request_or_user.META.get("REMOTE_ADDR")
        elif request_or_user is not None:
            user = request_or_user
        return cls.objects.create(user=user, action=action, target=str(target)[:200], details=details, ip_address=ip)
