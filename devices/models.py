from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone


class Device(models.Model):
    DRIVER_ZK = "zk"
    DRIVER_SIMULATOR = "simulator"
    DRIVER_CHOICES = [
        (DRIVER_ZK, "ZKTeco device (TCP/IP)"),
        (DRIVER_SIMULATOR, "Simulator (virtual device for testing)"),
    ]
    STATUS_CHOICES = [("unknown", "Unknown"), ("online", "Online"), ("offline", "Offline")]

    name = models.CharField(max_length=60, unique=True)
    ip_address = models.CharField("IP address", max_length=100)
    port = models.PositiveIntegerField(default=4370)
    comm_password = models.PositiveIntegerField(
        "Communication password", default=0, help_text="Device comm key (0 = none)."
    )
    driver = models.CharField(max_length=20, choices=DRIVER_CHOICES, default=DRIVER_ZK)
    area = models.CharField(max_length=60, blank=True, default="Area1")
    is_active = models.BooleanField("Enabled", default=True, help_text="Include this device in syncs.")
    is_enroll_device = models.BooleanField(
        "Enroll machine", default=True,
        help_text="Users and fingerprints enrolled on this device are imported into the portal "
                  "(and copied to other devices). Normal machines only receive users.",
    )
    live_enabled = models.BooleanField(
        "Live punches", default=True,
        help_text="Keep a listening connection open so punches appear on the dashboard within seconds.",
    )
    read_only = models.BooleanField(
        "Write protection (read-only)", default=False,
        help_text="When on, the portal only READS from this device (attendance, users) and never writes to it.",
    )

    # Information reported by the device (refreshed on every connection).
    serial_number = models.CharField(max_length=60, blank=True)
    device_model = models.CharField(max_length=60, blank=True)
    platform = models.CharField(max_length=60, blank=True)
    firmware = models.CharField(max_length=60, blank=True)
    fp_version = models.CharField("Fingerprint algorithm", max_length=20, blank=True)
    face_version = models.CharField(max_length=20, blank=True)
    mac_address = models.CharField(max_length=30, blank=True)
    user_count = models.PositiveIntegerField(null=True, blank=True)
    admin_count = models.PositiveIntegerField(null=True, blank=True)
    fingerprint_count = models.PositiveIntegerField(null=True, blank=True)
    face_count = models.PositiveIntegerField(null=True, blank=True)
    card_count = models.PositiveIntegerField(null=True, blank=True)
    record_count = models.PositiveIntegerField(null=True, blank=True)
    user_capacity = models.PositiveIntegerField(null=True, blank=True)
    fingerprint_capacity = models.PositiveIntegerField(null=True, blank=True)
    record_capacity = models.PositiveIntegerField(null=True, blank=True)
    device_time = models.DateTimeField(null=True, blank=True)
    clock_offset_seconds = models.IntegerField(null=True, blank=True)

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="unknown")
    last_seen = models.DateTimeField(null=True, blank=True)
    last_error = models.TextField(blank=True)
    last_attendance_sync = models.DateTimeField(null=True, blank=True)
    last_user_sync = models.DateTimeField(null=True, blank=True)
    last_record_count = models.PositiveIntegerField(null=True, blank=True, editable=False)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse("devices:detail", args=[self.pk])

    @property
    def is_simulator(self):
        return self.driver == self.DRIVER_SIMULATOR

    @property
    def fingerprint_free(self):
        if self.fingerprint_capacity is None or self.fingerprint_count is None:
            return None
        return max(self.fingerprint_capacity - self.fingerprint_count, 0)

    @property
    def record_usage_percent(self):
        """How full the attendance memory is (0-100), or None if unknown."""
        if not self.record_capacity or self.record_count is None:
            return None
        return min(round(self.record_count * 100 / self.record_capacity), 100)

    @property
    def clock_drift_display(self):
        if self.clock_offset_seconds is None:
            return ""
        s = self.clock_offset_seconds
        return f"{'+' if s >= 0 else '-'}{abs(s) // 60}m {abs(s) % 60}s"


class DeviceLiveStatus(models.Model):
    """State of the live (real-time) punch listener, written only by the worker."""

    STATUS_CHOICES = [("off", "Off"), ("connecting", "Connecting"), ("connected", "Live"), ("error", "Disconnected")]
    device = models.OneToOneField(Device, on_delete=models.CASCADE, related_name="live")
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default="off")
    error = models.CharField(max_length=300, blank=True)
    connected_at = models.DateTimeField(null=True, blank=True)
    last_event_at = models.DateTimeField(null=True, blank=True)
    events_today = models.PositiveIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.device} live: {self.status}"

    @property
    def is_fresh(self):
        """The worker refreshes this row at least every ~30 s while running."""
        from django.utils import timezone
        return bool(self.updated_at and (timezone.now() - self.updated_at).total_seconds() < 90)


class DeviceRemoval(models.Model):
    """A deleted employee that must also be removed from a device (retried until done)."""

    STATUS_CHOICES = [("pending", "Waiting"), ("done", "Removed"), ("skipped", "Not removed"), ("failed", "Will retry")]
    removed = models.ForeignKey("employees.RemovedDeviceUser", on_delete=models.CASCADE, related_name="removals")
    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="removals")
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending")
    message = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["removed", "device"], name="uniq_removal_device")]

    def mark(self, status, message=""):
        self.status, self.message = status, message[:200]
        self.save(update_fields=["status", "message", "updated_at"])


class FingerprintReplacement(models.Model):
    """A fingerprint that was replaced by a new scan and must still be replaced on this device
    (old template removed, new one uploaded) - done by the next upload/sync to the device."""

    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="fingerprint_replacements")
    employee = models.ForeignKey("employees.Employee", on_delete=models.CASCADE, related_name="+")
    finger_index = models.PositiveSmallIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["device", "employee", "finger_index"],
                                               name="uniq_fingerprint_replacement")]


class DeviceUserLink(models.Model):
    """Which portal employee is stored on which device (and under which internal uid)."""

    device = models.ForeignKey(Device, on_delete=models.CASCADE, related_name="user_links")
    employee = models.ForeignKey("employees.Employee", on_delete=models.CASCADE, related_name="device_links")
    uid = models.PositiveIntegerField(help_text="Internal record number on the device.")
    device_name = models.CharField(max_length=60, blank=True)
    privilege = models.PositiveSmallIntegerField(default=0)
    card_number = models.PositiveBigIntegerField(default=0)
    fingers = models.CharField(max_length=40, blank=True, help_text="Comma-separated finger indexes on the device.")
    synced_at = models.DateTimeField(default=timezone.now)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["device", "employee"], name="uniq_device_employee")]

    def __str__(self):
        return f"{self.employee} @ {self.device} (uid {self.uid})"

    @property
    def finger_list(self):
        return [int(x) for x in self.fingers.split(",") if x != ""]


class DeviceJob(models.Model):
    TYPE_REFRESH = "refresh_info"
    TYPE_PULL_USERS = "pull_users"
    TYPE_PUSH_USERS = "push_users"
    TYPE_PREVIEW_PUSH = "preview_push"
    TYPE_SYNC_USERS = "sync_users"
    TYPE_PULL_ATTENDANCE = "pull_attendance"
    TYPE_SYNC_TIME = "sync_time"
    TYPE_PUSH_EMPLOYEE = "push_employee"
    TYPE_ENROLL = "enroll_finger"
    TYPE_REMOVE_USERS = "remove_users"
    TYPE_RESTART = "restart_device"
    TYPE_CLEAR_ATTENDANCE = "clear_attendance"
    TYPE_CHOICES = [
        (TYPE_REFRESH, "Test connection / refresh info"),
        (TYPE_PULL_USERS, "Download users & fingerprints"),
        (TYPE_PUSH_USERS, "Upload users & fingerprints"),
        (TYPE_PREVIEW_PUSH, "Preview upload (dry run)"),
        (TYPE_SYNC_USERS, "Sync users & fingerprints across devices"),
        (TYPE_PULL_ATTENDANCE, "Download attendance"),
        (TYPE_SYNC_TIME, "Sync device clock"),
        (TYPE_PUSH_EMPLOYEE, "Send employee to devices"),
        (TYPE_ENROLL, "Remote fingerprint enrollment"),
        (TYPE_REMOVE_USERS, "Remove deleted employees from devices"),
        (TYPE_RESTART, "Reboot device"),
        (TYPE_CLEAR_ATTENDANCE, "Clear attendance records on device"),
    ]
    STATUS_CHOICES = [
        ("pending", "Pending"),
        ("running", "Running"),
        ("success", "Success"),
        ("partial", "Partially done"),
        ("failed", "Failed"),
        ("cancelled", "Cancelled"),
    ]

    job_type = models.CharField(max_length=30, choices=TYPE_CHOICES)
    device = models.ForeignKey(
        Device, null=True, blank=True, on_delete=models.CASCADE, related_name="jobs",
        help_text="Empty = all active devices.",
    )
    employee = models.ForeignKey("employees.Employee", null=True, blank=True, on_delete=models.CASCADE, related_name="+")
    payload = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending", db_index=True)
    progress = models.CharField(max_length=200, blank=True)
    log = models.TextField(blank=True)
    result = models.JSONField(default=dict, blank=True)
    is_automatic = models.BooleanField(default=False)
    cancel_requested = models.BooleanField(default=False)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"#{self.pk} {self.get_job_type_display()}"

    def get_absolute_url(self):
        return reverse("devices:job_detail", args=[self.pk])

    @property
    def target_label(self):
        parts = []
        if self.device_id:
            parts.append(self.device.name)
        elif (self.payload or {}).get("device_ids"):  # reboot / clear records on the ticked devices
            names = Device.objects.filter(pk__in=self.payload["device_ids"]).order_by("name").values_list("name", flat=True)
            parts.append(", ".join(names) or "Selected devices")
        else:
            parts.append("All devices")
        if self.employee_id:
            parts.append(self.employee.name)
        return " / ".join(parts)

    @property
    def is_finished(self):
        return self.status in ("success", "partial", "failed", "cancelled")

    @property
    def duration(self):
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return None

    def set_state(self, **state):
        """Live, structured progress (used by the fingerprint enrollment screen)."""
        self.result = state
        DeviceJob.objects.filter(pk=self.pk).update(result=state)

    def write(self, message, progress=None):
        """Append a line to the job log (persisted immediately so the UI can follow along)."""
        stamp = timezone.localtime().strftime("%H:%M:%S")
        self.log += f"[{stamp}] {message}\n"
        fields = ["log"]
        if progress is not None:
            self.progress = progress[:200]
            fields.append("progress")
        DeviceJob.objects.filter(pk=self.pk).update(**{f: getattr(self, f) for f in fields})
