from django.conf import settings
from django.core.validators import RegexValidator
from django.db import models
from django.urls import reverse

# ZKTeco privilege levels (pyzk const.USER_*).
PRIVILEGE_CHOICES = [
    (0, "Normal user"),
    (2, "Enroller"),
    (6, "Manager"),
    (14, "Super admin"),
]

FINGER_NAMES = {
    0: "Left little", 1: "Left ring", 2: "Left middle", 3: "Left index", 4: "Left thumb",
    5: "Right thumb", 6: "Right index", 7: "Right middle", 8: "Right ring", 9: "Right little",
}
FINGER_CHOICES = sorted(FINGER_NAMES.items())

DEVICE_NAME_BYTES = 24  # ZK 72-byte user record stores 24 bytes of name


def device_name_for(name):
    """Name as the device would store it (UTF-8, max 24 bytes, no broken characters)."""
    raw = (name or "").strip().encode("utf-8")[:DEVICE_NAME_BYTES]
    return raw.decode("utf-8", errors="ignore").strip()


class Department(models.Model):
    name = models.CharField(max_length=100, unique=True)
    code = models.CharField(max_length=20, blank=True)
    description = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Designation(models.Model):
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Employee(models.Model):
    STATUS_ACTIVE = "active"
    STATUS_INACTIVE = "inactive"
    STATUS_CHOICES = [(STATUS_ACTIVE, "Active"), (STATUS_INACTIVE, "Inactive / left")]

    device_user_id = models.CharField(
        "Employee ID (device PIN)", max_length=24, unique=True,
        help_text="The user ID stored on the biometric devices. Must be the same on every device.",
    )
    name = models.CharField(max_length=120)
    department = models.ForeignKey(Department, null=True, blank=True, on_delete=models.SET_NULL, related_name="employees")
    designation = models.ForeignKey(Designation, null=True, blank=True, on_delete=models.SET_NULL, related_name="employees")
    shift = models.ForeignKey(
        "attendance.Shift", null=True, blank=True, on_delete=models.SET_NULL, related_name="employees",
        help_text="Leave empty to use the default shift.",
    )
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=30, blank=True)
    join_date = models.DateField(null=True, blank=True)
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default=STATUS_ACTIVE, db_index=True)

    # Data stored on the device.
    card_number = models.PositiveBigIntegerField("RFID card number", default=0, blank=True)
    device_password = models.CharField(
        "Device password", max_length=8, blank=True,
        validators=[RegexValidator(r"^[0-9]*$", "Digits only.")],
    )
    privilege = models.PositiveSmallIntegerField("Device privilege", choices=PRIVILEGE_CHOICES, default=0)

    sync_to_all_devices = models.BooleanField(
        default=True, help_text="Register this employee on every active device.",
    )
    devices = models.ManyToManyField(
        "devices.Device", blank=True, related_name="assigned_employees",
        help_text="Only used when 'sync to all devices' is off.",
    )

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL, related_name="employee",
        help_text="Self-service login account.",
    )
    source_device = models.ForeignKey(
        "devices.Device", null=True, blank=True, on_delete=models.SET_NULL, related_name="+",
        help_text="Device this employee was first imported from.",
    )
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return f"{self.name} ({self.device_user_id})"

    def get_absolute_url(self):
        return reverse("employees:detail", args=[self.pk])

    @property
    def is_active(self):
        return self.status == self.STATUS_ACTIVE

    @property
    def device_name(self):
        return device_name_for(self.name)

    def get_shift(self):
        if self.shift_id:
            return self.shift
        from core.models import SystemSetting
        return SystemSetting.get().default_shift

    @property
    def shift_label(self):
        if self.shift_id:
            return str(self.shift)
        from core.models import SystemSetting
        settings_obj = SystemSetting.get()
        if settings_obj.auto_shift_detection:
            return "Auto-detect from punches"
        return str(settings_obj.default_shift or "—")

    def target_devices(self):
        """Devices this employee should be registered on."""
        from devices.models import Device
        qs = Device.objects.filter(is_active=True)
        if not self.sync_to_all_devices:
            qs = qs.filter(pk__in=self.devices.values("pk"))
        return qs


class FingerprintTemplate(models.Model):
    """Fingerprint template collected from a device, used to copy enrollment to other devices."""

    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name="fingerprints")
    finger_index = models.PositiveSmallIntegerField(choices=FINGER_CHOICES)
    template = models.BinaryField()
    valid = models.SmallIntegerField(default=1)
    size = models.PositiveIntegerField(default=0)
    source_device = models.ForeignKey("devices.Device", null=True, blank=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["employee", "finger_index"]
        constraints = [models.UniqueConstraint(fields=["employee", "finger_index"], name="uniq_employee_finger")]

    def __str__(self):
        return f"{self.employee} - {self.finger_name}"

    @property
    def finger_name(self):
        return FINGER_NAMES.get(self.finger_index, f"Finger {self.finger_index}")


class RemovedDeviceUser(models.Model):
    """An employee deleted from the portal. The person may still exist on the devices (the portal never
    deletes from devices), so their user ID is remembered to stop syncs from importing them again."""

    device_user_id = models.CharField(max_length=24, unique=True)
    name = models.CharField(max_length=120)
    department = models.CharField(max_length=100, blank=True)
    removed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    removed_at = models.DateTimeField(auto_now_add=True)
    remove_from_devices = models.BooleanField(default=False)
    backup = models.JSONField(default=dict, blank=True, help_text="Employee details and fingerprints, for Restore.")

    class Meta:
        ordering = ["-removed_at"]

    def __str__(self):
        return f"{self.name} ({self.device_user_id}) - removed"
