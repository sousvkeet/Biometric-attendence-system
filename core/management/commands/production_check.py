"""Go-live checklist: `manage.py production_check` (exit code 1 when something must be fixed)."""
import os
import shutil
import socket
import stat

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone


class Command(BaseCommand):
    help = "Check that this installation is safe and complete for production use."

    def add_arguments(self, parser):
        parser.add_argument("--skip-devices", action="store_true", help="Do not test network access to the devices.")

    def handle(self, *args, **opts):
        self.counts = {"PASS": 0, "WARN": 0, "FAIL": 0}
        self.section("Configuration")
        self.verify(not settings.DEBUG, "DEBUG is off", "DEBUG is on: error pages reveal code and settings (set DEBUG=false)")
        self.verify("*" not in settings.ALLOWED_HOSTS, f"ALLOWED_HOSTS = {', '.join(settings.ALLOWED_HOSTS)}",
                   "ALLOWED_HOSTS is '*': list the names/IPs used to open the portal", level="WARN")
        https = getattr(settings, "SESSION_COOKIE_SECURE", False)
        self.verify(https, "HTTPS mode on (secure cookies, HSTS)",
                   "HTTPS is off: passwords/cookies travel unencrypted on the network (see docs/DEPLOYMENT.md → HTTPS)",
                   level="WARN")
        if https:
            self.verify(bool(settings.CSRF_TRUSTED_ORIGINS), "CSRF_TRUSTED_ORIGINS set",
                       "Set CSRF_TRUSTED_ORIGINS=https://your-portal-address (forms fail otherwise)")
        deploy_issues = self._deploy_check()
        self.verify(not deploy_issues, "Django deployment check clean",
                   f"Django deployment check: {deploy_issues}", level="WARN")
        self.verify(bool(getattr(settings, "ADMINS", None)), "Error e-mails go to " +
                   ", ".join(a[1] for a in getattr(settings, "ADMINS", [])),
                   "No error e-mails configured (optional: EMAIL_HOST, ADMIN_EMAILS)", level="INFO")
        for path in (settings.BASE_DIR / ".env", settings.DATABASES["default"].get("NAME")):
            if path and os.path.isfile(str(path)):
                mode = os.stat(path).st_mode
                self.verify(not mode & (stat.S_IRGRP | stat.S_IROTH), f"{os.path.basename(str(path))} is private",
                           f"{path} is readable by other users: chmod 600 {path}")

        self.section("Database")
        try:
            connection.ensure_connection()
            engine = connection.vendor
            self.verify(True, f"Connected ({engine})", "")
            executor = MigrationExecutor(connection)
            pending = executor.migration_plan(executor.loader.graph.leaf_nodes())
            self.verify(not pending, "All migrations applied", f"{len(pending)} migration(s) pending: run 'manage.py migrate'")
            if engine == "sqlite":
                self.out("INFO", "SQLite is fine for one site; PostgreSQL is recommended for larger or multi-server use")
        except Exception as exc:
            self.verify(False, "", f"Cannot connect to the database: {exc}")
            return self.summary()

        from django.contrib.auth import get_user_model
        U = get_user_model()
        self.verify(U.objects.filter(is_superuser=True, is_active=True).exists(), "A main administrator exists",
                   "No active superuser: run 'manage.py createsuperuser'")

        self.section("Background worker and devices")
        from core.models import SystemSetting
        from devices.models import Device
        self.verify(SystemSetting.get().worker_online, "Worker is running (heartbeat < 1 min)",
                   "Worker is not running: start 'manage.py run_worker' (systemd/pm2/docker service)")
        devices = list(Device.objects.filter(is_active=True, driver=Device.DRIVER_ZK))
        self.verify(bool(devices), f"{len(devices)} active device(s)", "No active devices configured", level="WARN")
        for d in devices:
            if not opts["skip_devices"]:
                try:
                    socket.create_connection((d.ip_address, d.port), timeout=4).close()
                    self.out("PASS", f"{d.name}: {d.ip_address}:{d.port} reachable")
                except OSError as exc:
                    self.out("FAIL", f"{d.name}: {d.ip_address}:{d.port} NOT reachable ({exc})")
            if d.clock_offset_seconds is not None and abs(d.clock_offset_seconds) > 120:
                self.out("WARN", f"{d.name}: clock is off by {d.clock_drift_display} (Devices → Sync clock)")

        self.section("Backups")
        from backups.models import BackupDestination, BackupRun, BackupSettings
        cfg = BackupSettings.get()
        dests = list(BackupDestination.objects.filter(enabled=True))
        self.verify(bool(dests), f"{len(dests)} backup destination(s)", "No backup destinations (Setup → Backups)")
        self.verify(cfg.enabled, f"Automatic backups on ({cfg.get_frequency_display().lower()})", "Automatic backups are off")
        self.verify(cfg.encrypt and bool(cfg.passphrase_encrypted), "Backups are encrypted",
                   "Backups are not encrypted (they contain personal and fingerprint data)", level="WARN")
        if dests:
            self.verify(any(d.kind != "local" for d in dests), "An off-site destination exists",
                       "Only local destinations: add an off-site one (S3, SFTP, rclone…) for disasters", level="WARN")
            if any(d.kind == "rclone" for d in dests):
                self.verify(bool(shutil.which(settings.RCLONE_BINARY) or os.path.exists(settings.RCLONE_BINARY)),
                           "rclone installed", f"rclone not found ({settings.RCLONE_BINARY})")
        last = BackupRun.objects.filter(status="success").order_by("-finished_at").first()
        recent = last and (timezone.now() - last.finished_at).days < 2
        when = f"{timezone.localtime(last.finished_at):%d %b %Y %H:%M}" if last else ""
        self.verify(bool(recent), f"Last successful backup: {when}", "No successful backup in the last 2 days")

        self.section("Server")
        usage = shutil.disk_usage(settings.VAR_DIR if os.path.isdir(settings.VAR_DIR) else settings.BASE_DIR)
        free_gb = usage.free / 1024 ** 3
        self.verify(free_gb > 2, f"{free_gb:.1f} GB free disk space", f"Only {free_gb:.1f} GB free disk space",
                   level="WARN" if free_gb > 0.5 else "FAIL")
        self.out("INFO", f"Time zone {settings.TIME_ZONE}, server time {timezone.localtime():%Y-%m-%d %H:%M}")
        return self.summary()

    # ---------------------------------------------------------------- helpers
    def _deploy_check(self):
        from io import StringIO
        out = StringIO()
        try:
            call_command("check", deploy=True, stdout=out, stderr=out)
        except Exception as exc:  # SystemCheckError
            out.write(str(exc))
        text = out.getvalue()
        ids = sorted(set(part.split(")")[0] for part in text.split("(security.")[1:]))
        return ", ".join(ids)

    def section(self, title):
        self.stdout.write(self.style.MIGRATE_HEADING(f"\n{title}"))

    def out(self, level, message):
        self.counts[level] = self.counts.get(level, 0) + 1
        style = {"PASS": self.style.SUCCESS, "WARN": self.style.WARNING, "FAIL": self.style.ERROR}.get(level, str)
        self.stdout.write(f"  {style(level.ljust(4))}  {message}")

    def verify(self, ok, good, bad, level="FAIL"):
        self.out("PASS" if ok else level, good if ok else bad)

    def summary(self):
        c = self.counts
        self.stdout.write(f"\n{c['PASS']} passed, {c['WARN']} warnings, {c['FAIL']} problems")
        if c["FAIL"]:
            raise SystemExit(1)
