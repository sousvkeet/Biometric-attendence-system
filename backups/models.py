import datetime as dt

from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone

from core.secrets import decrypt_secret, encrypt_secret


class BackupDestination(models.Model):
    LOCAL, S3, FTP, FTPS, SFTP, WEBDAV, RCLONE = "local", "s3", "ftp", "ftps", "sftp", "webdav", "rclone"
    KIND_CHOICES = [
        (LOCAL, "Local folder / mounted network drive"),
        (S3, "Amazon S3 or S3-compatible (Wasabi, MinIO, Backblaze B2, DO Spaces, R2)"),
        (FTP, "FTP"),
        (FTPS, "FTPS (FTP over TLS)"),
        (SFTP, "SFTP (SSH)"),
        (WEBDAV, "WebDAV (Nextcloud, ownCloud, Synology…)"),
        (RCLONE, "rclone (Google Drive, OneDrive, Dropbox, Box, Mega, Azure… 70+ providers)"),
    ]
    KIND_ICONS = {LOCAL: "bi-hdd", S3: "bi-bucket", FTP: "bi-hdd-network", FTPS: "bi-shield-lock",
                  SFTP: "bi-terminal", WEBDAV: "bi-cloud", RCLONE: "bi-cloud-haze2"}

    name = models.CharField(max_length=80)
    kind = models.CharField("Type", max_length=10, choices=KIND_CHOICES)
    enabled = models.BooleanField(default=True)

    # Shared
    host = models.CharField(max_length=200, blank=True, help_text="Server name or IP address.")
    port = models.PositiveIntegerField(null=True, blank=True, help_text="Leave empty for the default port.")
    username = models.CharField(max_length=150, blank=True)
    path = models.CharField(
        "Folder / prefix", max_length=300, blank=True,
        help_text="Folder on the server (or key prefix for S3). For 'Local folder' this is the full path.",
    )
    # S3
    bucket = models.CharField(max_length=200, blank=True)
    region = models.CharField(max_length=60, blank=True, help_text="e.g. ap-south-1 (S3-compatible: often empty).")
    endpoint_url = models.CharField(
        "Endpoint URL", max_length=300, blank=True,
        help_text="Only for S3-compatible services, e.g. https://s3.wasabisys.com or http://minio:9000.",
    )
    access_key = models.CharField("Access key ID", max_length=200, blank=True)
    # WebDAV
    url = models.CharField("WebDAV URL", max_length=400, blank=True,
                           help_text="e.g. https://cloud.example.com/remote.php/dav/files/USERNAME/")
    # rclone
    rclone_remote = models.CharField(
        "rclone remote name", max_length=100, blank=True,
        help_text="Name of the remote in the rclone config, e.g. gdrive (without the colon).",
    )
    rclone_config_encrypted = models.TextField(blank=True, editable=False)
    # Options
    passive = models.BooleanField("Passive mode (FTP)", default=True)
    verify_tls = models.BooleanField("Verify TLS certificate", default=True)
    keep_last = models.PositiveIntegerField(
        "Keep last N backups", default=14,
        help_text="Older backup files created by this system are removed from this destination. 0 = keep all.",
    )
    # Encrypted credentials
    secret_encrypted = models.TextField(blank=True, editable=False)       # password / S3 secret key
    private_key_encrypted = models.TextField(blank=True, editable=False)  # SFTP private key
    key_passphrase_encrypted = models.TextField(blank=True, editable=False)
    host_key = models.TextField(blank=True, editable=False, help_text="SFTP server key remembered on first connection.")

    last_status = models.CharField(max_length=10, blank=True)
    last_message = models.CharField(max_length=300, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name

    @property
    def icon(self):
        return self.KIND_ICONS.get(self.kind, "bi-hdd")

    @property
    def target_label(self):
        if self.kind == self.LOCAL:
            return self.path
        if self.kind == self.S3:
            return f"s3://{self.bucket}/{self.path}".rstrip("/") + (f" @ {self.endpoint_url}" if self.endpoint_url else "")
        if self.kind == self.WEBDAV:
            return (self.url.rstrip("/") + "/" + self.path.strip("/")).rstrip("/")
        if self.kind == self.RCLONE:
            return f"rclone {self.rclone_remote}:{self.path.strip('/')}"
        port = f":{self.port}" if self.port else ""
        return f"{self.kind}://{self.username + '@' if self.username else ''}{self.host}{port}/{self.path.strip('/')}"

    # credentials
    @property
    def secret(self):
        return decrypt_secret(self.secret_encrypted)

    @secret.setter
    def secret(self, value):
        self.secret_encrypted = encrypt_secret(value)

    @property
    def private_key(self):
        return decrypt_secret(self.private_key_encrypted)

    @private_key.setter
    def private_key(self, value):
        self.private_key_encrypted = encrypt_secret(value)

    @property
    def rclone_config(self):
        return decrypt_secret(self.rclone_config_encrypted)

    @rclone_config.setter
    def rclone_config(self, value):
        self.rclone_config_encrypted = encrypt_secret(value)

    @property
    def key_passphrase(self):
        return decrypt_secret(self.key_passphrase_encrypted)

    @key_passphrase.setter
    def key_passphrase(self, value):
        self.key_passphrase_encrypted = encrypt_secret(value)


class BackupSettings(models.Model):
    """Singleton (pk=1)."""

    FREQ_CHOICES = [("daily", "Every day"), ("12h", "Every 12 hours"), ("6h", "Every 6 hours"), ("weekly", "Every week")]
    WEEKDAYS = [(0, "Monday"), (1, "Tuesday"), (2, "Wednesday"), (3, "Thursday"), (4, "Friday"), (5, "Saturday"), (6, "Sunday")]

    enabled = models.BooleanField("Automatic backups", default=False)
    frequency = models.CharField(max_length=10, choices=FREQ_CHOICES, default="daily")
    time_of_day = models.TimeField("Time", default=dt.time(2, 0), help_text="Daily/weekly: when to run. 6/12 h: first run of the day.")
    weekday = models.PositiveSmallIntegerField(choices=WEEKDAYS, default=6, help_text="Weekly backups only.")
    encrypt = models.BooleanField(
        "Encrypt backup files", default=True,
        help_text="Recommended: backups contain personal data and fingerprint templates.",
    )
    passphrase_encrypted = models.TextField(blank=True, editable=False)
    include_logins = models.BooleanField(
        "Include user accounts & passwords", default=False,
        help_text="Off: backups contain everything except login details (user accounts, password hashes, sessions); "
                  "restoring keeps the current logins. On: complete backup including logins.",
    )
    last_scheduled_at = models.DateTimeField(null=True, blank=True, editable=False)

    def __str__(self):
        return "Backup settings"

    @classmethod
    def get(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj

    @property
    def passphrase(self):
        return decrypt_secret(self.passphrase_encrypted)

    @passphrase.setter
    def passphrase(self, value):
        self.passphrase_encrypted = encrypt_secret(value)

    def next_run_after(self, after):
        """Next scheduled moment (aware) strictly after `after`."""
        local = timezone.localtime(after)
        base = local.replace(hour=self.time_of_day.hour, minute=self.time_of_day.minute, second=0, microsecond=0)
        if self.frequency in ("6h", "12h"):
            step = dt.timedelta(hours=6 if self.frequency == "6h" else 12)
            candidate = base - dt.timedelta(days=1)
            while candidate <= local:
                candidate += step
            return candidate
        if self.frequency == "weekly":
            candidate = base + dt.timedelta(days=(self.weekday - base.weekday()) % 7)
            if candidate <= local:
                candidate += dt.timedelta(days=7)
            return candidate
        candidate = base if base > local else base + dt.timedelta(days=1)
        return candidate


class BackupRun(models.Model):
    STATUS_CHOICES = [("pending", "Waiting"), ("running", "Running"), ("success", "Success"),
                      ("partial", "Partially done"), ("failed", "Failed")]
    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default="pending", db_index=True)
    trigger = models.CharField(max_length=20, default="manual")  # manual / scheduled / pre-restore
    file_name = models.CharField(max_length=200, blank=True)
    size = models.BigIntegerField(null=True, blank=True)
    sha256 = models.CharField(max_length=64, blank=True)
    encrypted = models.BooleanField(default=False)
    results = models.JSONField(default=dict, blank=True)  # {destination id: {name, ok, message}}
    log = models.TextField(blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True, on_delete=models.SET_NULL)
    created_at = models.DateTimeField(auto_now_add=True)
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        return f"Backup #{self.pk}"

    def get_absolute_url(self):
        return reverse("backups:run", args=[self.pk])

    @property
    def is_finished(self):
        return self.status in ("success", "partial", "failed")

    @property
    def duration(self):
        if self.started_at and self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return None

    def write(self, message):
        stamp = timezone.localtime().strftime("%H:%M:%S")
        self.log += f"[{stamp}] {message}\n"
        BackupRun.objects.filter(pk=self.pk).update(log=self.log)
