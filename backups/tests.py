import datetime as dt
import os
import shutil
import tempfile
import time

from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from employees.models import Employee

from .engine import (BackupError, claim_pending_run, create_backup_file, decrypt_file, encrypt_file,
                     prepare_restore_file, run_backup)
from .forms import DestinationForm
from .models import BackupDestination, BackupRun, BackupSettings
from .transports import BACKUP_NAME_RE, transport_for


def _db_kind():
    from django.db import connection
    return "sqlite" if connection.vendor == "sqlite" else "postgres"


def _db_ext():
    return ".sqlite3.gz" if _db_kind() == "sqlite" else ".pgdump"


class TempDirMixin:
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp()
        self.override = override_settings(BACKUP_WORK_DIR=os.path.join(self.tmp, "work"))
        self.override.enable()
        self.store = os.path.join(self.tmp, "store")

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.tmp, ignore_errors=True)
        super().tearDown()


class EncryptionTests(TempDirMixin, TestCase):
    def test_roundtrip_and_wrong_passphrase(self):
        src = os.path.join(self.tmp, "plain")
        data = os.urandom(5 * 1024 * 1024 + 123)  # crosses the 4 MB chunk boundary
        with open(src, "wb") as fh:
            fh.write(data)
        enc, out = src + ".enc", src + ".out"
        encrypt_file(src, enc, "correct horse")
        self.assertNotIn(data[:64], open(enc, "rb").read())
        decrypt_file(enc, out, "correct horse")
        self.assertEqual(open(out, "rb").read(), data)
        with self.assertRaises(BackupError):
            decrypt_file(enc, out, "wrong passphrase")


class BackupRunTests(TempDirMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        Employee.objects.create(device_user_id="1", name="Backup Test")
        self.dest = BackupDestination.objects.create(name="Local", kind="local", path=self.store, keep_last=3)
        cfg = BackupSettings.get()
        cfg.encrypt = True
        cfg.passphrase = "s3cret-pass"
        cfg.save()

    def test_full_backup_encrypted_and_restorable(self):
        run = BackupRun.objects.create(status="running", started_at=timezone.now())
        run_backup(run)
        self.assertEqual(run.status, "success", run.log)
        self.assertTrue(run.encrypted)
        self.assertTrue(BACKUP_NAME_RE.match(run.file_name))
        path = os.path.join(self.store, run.file_name)
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(len(run.sha256), 64)
        work = tempfile.mkdtemp(dir=self.tmp)
        kind, prepared = prepare_restore_file(path, "s3cret-pass", work, log=lambda m: None)
        self.assertEqual(kind, _db_kind())
        if _db_kind() == "sqlite":
            import sqlite3
            con = sqlite3.connect(prepared)
            self.assertEqual(con.execute("SELECT name FROM employees_employee").fetchall(), [("Backup Test",)])
            con.close()
        else:
            import subprocess
            dump = subprocess.run(["pg_restore", "--data-only", "-t", "employees_employee", "-f", "-", prepared],
                                  capture_output=True, text=True, check=True).stdout
            self.assertIn("Backup Test", dump)
        with self.assertRaises(BackupError):
            prepare_restore_file(path, "wrong", tempfile.mkdtemp(dir=self.tmp), log=lambda m: None)

    def test_retention_only_removes_own_old_backups(self):
        os.makedirs(self.store)
        old = [f"bioattend-backup-2020010{i}-000000.sqlite3.gz" for i in range(1, 6)]
        for name in old + ["important-report.xlsx", "bioattend-notes.txt"]:
            open(os.path.join(self.store, name), "w").close()
        run = BackupRun.objects.create(status="running", started_at=timezone.now())
        run_backup(run)
        left = sorted(os.listdir(self.store))
        self.assertIn("important-report.xlsx", left)
        self.assertIn("bioattend-notes.txt", left)
        backups = [n for n in left if BACKUP_NAME_RE.match(n)]
        self.assertEqual(len(backups), 3)
        self.assertIn(run.file_name, backups)
        self.assertNotIn(old[0], backups)

    def test_unencrypted_and_partial_failure(self):
        cfg = BackupSettings.get()
        cfg.encrypt = False
        cfg.save()
        BackupDestination.objects.create(name="Broken FTP", kind="ftp", host="127.0.0.1", port=1)
        run = BackupRun.objects.create(status="running", started_at=timezone.now())
        run_backup(run)
        self.assertEqual(run.status, "partial")
        self.assertTrue(run.file_name.endswith(_db_ext()))
        ok = {r["name"]: r["ok"] for r in run.results.values()}
        self.assertEqual(ok, {"Local": True, "Broken FTP": False})

    def test_no_destination_fails_cleanly(self):
        self.dest.delete()
        run = BackupRun.objects.create(status="running", started_at=timezone.now())
        run_backup(run)
        self.assertEqual(run.status, "failed")
        self.assertIn("No enabled backup destinations", run.log)

    def test_delete_refuses_foreign_files(self):
        from .transports import TransportError
        with self.assertRaises(TransportError):
            transport_for(self.dest).delete("important-report.xlsx")


class ScheduleTests(TestCase):
    def test_next_run(self):
        cfg = BackupSettings(frequency="daily", time_of_day=dt.time(2, 0))
        tz = timezone.get_current_timezone()
        at = timezone.make_aware(dt.datetime(2026, 10, 2, 1, 0), tz)
        self.assertEqual(timezone.localtime(cfg.next_run_after(at)).replace(tzinfo=None), dt.datetime(2026, 10, 2, 2, 0))
        at = timezone.make_aware(dt.datetime(2026, 10, 2, 3, 0), tz)
        self.assertEqual(timezone.localtime(cfg.next_run_after(at)).replace(tzinfo=None), dt.datetime(2026, 10, 3, 2, 0))
        cfg.frequency = "6h"
        self.assertEqual(timezone.localtime(cfg.next_run_after(at)).replace(tzinfo=None), dt.datetime(2026, 10, 2, 8, 0))
        cfg.frequency, cfg.weekday = "weekly", 6  # Sunday
        self.assertEqual(timezone.localtime(cfg.next_run_after(at)).replace(tzinfo=None), dt.datetime(2026, 10, 4, 2, 0))


class FormAndViewTests(TempDirMixin, TransactionTestCase):
    def setUp(self):
        super().setUp()
        User = get_user_model()
        self.admin = User.objects.create_superuser("root", "", "pw123456")
        self.staff = User.objects.create_user("hr", password="pw123456", is_staff=True)

    def test_credentials_are_encrypted_and_kept(self):
        form = DestinationForm({"name": "NAS", "kind": "sftp", "enabled": "on", "host": "nas", "username": "bk",
                                "password": "TopSecret!", "path": "backups", "keep_last": 7})
        self.assertTrue(form.is_valid(), form.errors)
        dest = form.save()
        raw = BackupDestination.objects.filter(pk=dest.pk).values_list("secret_encrypted", flat=True).get()
        self.assertNotIn("TopSecret", raw)
        self.assertEqual(BackupDestination.objects.get(pk=dest.pk).secret, "TopSecret!")
        form = DestinationForm({"name": "NAS", "kind": "sftp", "enabled": "on", "host": "nas", "username": "bk",
                                "password": "", "path": "backups", "keep_last": 7}, instance=dest)
        self.assertTrue(form.is_valid(), form.errors)
        form.save()
        self.assertEqual(BackupDestination.objects.get(pk=dest.pk).secret, "TopSecret!")

    def test_required_fields_per_type(self):
        form = DestinationForm({"name": "S3", "kind": "s3", "keep_last": 3})
        self.assertFalse(form.is_valid())
        for field in ("bucket", "access_key", "secret_key"):
            self.assertIn(field, form.errors)

    def test_superuser_only_and_flow(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("backups:index")).status_code, 403)
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get(reverse("backups:index")).status_code, 200)
        self.client.post(reverse("backups:destination_create"), {"name": "Local", "kind": "local", "enabled": "on",
                                                                 "path": self.store, "keep_last": 5})
        dest = BackupDestination.objects.get(name="Local")
        resp = self.client.post(reverse("backups:destination_test", args=[dest.pk]), follow=True)
        self.assertContains(resp, "Connected")
        self.assertEqual(os.listdir(self.store), [])  # test file cleaned up
        self.client.post(reverse("backups:index"), {"frequency": "daily", "time_of_day": "02:00", "weekday": 6})
        resp = self.client.post(reverse("backups:now"))
        run = BackupRun.objects.get()
        self.assertRedirects(resp, run.get_absolute_url(), fetch_redirect_response=False)
        claimed = claim_pending_run()
        run_backup(claimed)
        run.refresh_from_db()
        self.assertEqual(run.status, "success", run.log)
        files = self.client.get(reverse("backups:files", args=[dest.pk]))
        self.assertContains(files, run.file_name)
        download = self.client.get(reverse("backups:download", args=[dest.pk, run.file_name]))
        self.assertEqual(download.status_code, 200)
        self.assertGreater(len(b"".join(download.streaming_content)), 1000)
        self.assertEqual(self.client.get(reverse("backups:download", args=[dest.pk, "..%2Fetc%2Fpasswd"])).status_code, 404)


import shutil as _shutil
import unittest as _unittest

from django.conf import settings as _settings


@_unittest.skipUnless(_shutil.which(_settings.RCLONE_BINARY) or os.path.exists(_settings.RCLONE_BINARY), "rclone not installed")
class RcloneTests(TempDirMixin, TransactionTestCase):
    """Runs the real rclone binary against an 'alias' remote pointing at a temporary folder."""

    def make_dest(self, **kw):
        from .models import BackupDestination
        self.store = os.path.join(self.tmp, "remote-store")
        os.makedirs(self.store)
        d = BackupDestination(name="rclone test", kind="rclone", rclone_remote="testremote", path="Backups/BioAttend",
                              keep_last=2, **kw)
        d.rclone_config = f"[testremote]\ntype = alias\nremote = {self.store}\n"
        d.save()
        return d

    def test_backup_retention_download_and_restore_file(self):
        from .engine import prepare_restore_file, run_backup, sha256_of
        from .models import BackupRun, BackupSettings
        from .transports import transport_for
        d = self.make_dest()
        self.assertIn("Connected", transport_for(d).test())
        cfg = BackupSettings.get()
        cfg.encrypt, cfg.passphrase = True, "rclone-pass-123"
        cfg.save()
        runs = []
        for _ in range(3):
            r = BackupRun.objects.create(status="running", started_at=timezone.now())
            run_backup(r)
            self.assertEqual(r.status, "success", r.log)
            runs.append(r)
            time.sleep(1.1)
        folder = os.path.join(self.store, "Backups", "BioAttend")
        self.assertEqual(sorted(os.listdir(folder)), [runs[1].file_name, runs[2].file_name])
        t = transport_for(d)
        self.assertEqual(t.list_backups(), [runs[1].file_name, runs[2].file_name])
        local = os.path.join(self.tmp, runs[2].file_name)
        t.download(runs[2].file_name, local)
        self.assertEqual(sha256_of(local), runs[2].sha256)
        kind, _ = prepare_restore_file(local, "rclone-pass-123", self.tmp, lambda m: None)
        self.assertEqual(kind, _db_kind())
        leftovers = [f for f in os.listdir(os.path.join(_settings.BACKUP_WORK_DIR, "tmp")) if f.startswith("rclone-")]
        self.assertEqual(leftovers, [])  # temporary config files removed

    def test_errors_are_clear(self):
        from .transports import TransportError, transport_for
        d = self.make_dest()
        d.rclone_remote = "missing"
        with self.assertRaises(TransportError) as ctx:
            transport_for(d).test()
        self.assertIn("rclone:", str(ctx.exception))
        d.rclone_remote = "bad;name"
        with self.assertRaises(TransportError):
            transport_for(d).test()

    def test_refreshed_token_is_saved_back(self):
        from .models import BackupDestination
        from .transports import transport_for
        fake = os.path.join(self.tmp, "fake-rclone")
        with open(fake, "w") as fh:
            fh.write('#!/bin/sh\nconf="$2"\necho "token = {\\"access_token\\":\\"NEW\\"}" >> "$conf"\n')
        os.chmod(fake, 0o755)
        d = self.make_dest()
        with override_settings(RCLONE_BINARY=fake):
            transport_for(d).list_backups()
        self.assertIn('"NEW"', BackupDestination.objects.get(pk=d.pk).rclone_config)

    def test_form_validation(self):
        from .forms import DestinationForm
        base = {"name": "GD", "kind": "rclone", "enabled": "on", "keep_last": 5, "passive": "on", "verify_tls": "on"}
        f = DestinationForm(data={**base, "rclone_remote": "gdrive"})
        self.assertIn("rclone_config", f.errors)
        f = DestinationForm(data={**base, "rclone_remote": "gdrive", "rclone_config": "[other]\ntype = drive"})
        self.assertIn("rclone_config", f.errors)
        f = DestinationForm(data={**base, "rclone_remote": "gdrive:", "rclone_config": "[gdrive]\ntype = drive\n"})
        self.assertTrue(f.is_valid(), f.errors)
        d = f.save()
        self.assertEqual(d.rclone_remote, "gdrive")
        self.assertNotIn("drive", d.rclone_config_encrypted)  # stored encrypted
        self.assertIn("type = drive", d.rclone_config)


from unittest import mock as _mock


class WebRestoreTests(TempDirMixin, TransactionTestCase):
    """Full restore flow; the background process is run in-process."""

    def setUp(self):
        super().setUp()
        from . import restore
        self.patches = [_mock.patch.object(restore, "SETTLE_SECONDS", 0),
                        _mock.patch.object(restore, "launch", lambda passphrase: restore.run_restore_job(passphrase))]
        for p in self.patches:
            p.start()
        self.admin = get_user_model().objects.create_superuser("admin", "", "pw123456")
        self.client.force_login(self.admin)
        self.dest = BackupDestination.objects.create(name="Local", kind="local", path=self.store, keep_last=5)
        cfg = BackupSettings.get()
        cfg.encrypt, cfg.passphrase = True, "restore-pass-123"
        cfg.save()
        self.emp = Employee.objects.create(device_user_id="1", name="Original Name")
        from .engine import run_backup
        run = BackupRun.objects.create(status="running", started_at=timezone.now())
        run_backup(run)
        self.assertEqual(run.status, "success", run.log)
        self.file = run.file_name
        time.sleep(1.1)  # next backup gets another timestamp
        # changes made after the backup
        Employee.objects.filter(pk=self.emp.pk).update(name="Changed Later")
        Employee.objects.create(device_user_id="2", name="Added Later")

    def tearDown(self):
        for p in self.patches:
            p.stop()
        super().tearDown()

    def restore(self, **extra):
        data = {"dest": self.dest.pk, "file": self.file, "password": "pw123456", "confirm": "RESTORE", **extra}
        return self.client.post(reverse("backups:restore"), data)

    def test_restore_from_destination(self):
        from . import maintenance
        from devices.models import DeviceJob
        page = self.client.get(reverse("backups:restore"), {"dest": self.dest.pk, "file": self.file})
        self.assertContains(page, "Type <code>RESTORE</code>")
        self.restore(backup_first="on")
        status = maintenance.read_status()
        self.assertTrue(status["ok"], status)
        self.assertFalse(maintenance.is_active())
        self.assertEqual(Employee.objects.get(device_user_id="1").name, "Original Name")
        self.assertFalse(Employee.objects.filter(device_user_id="2").exists())
        self.assertTrue(DeviceJob.objects.filter(job_type="pull_attendance", payload={"force": True}).exists())
        from .engine import list_safety_copies
        self.assertEqual(len(list_safety_copies()), 1)
        backups_now = transport_for(self.dest).list_backups()
        self.assertEqual(len(backups_now), 2)  # the pre-restore backup of the current data was uploaded
        pre = BackupRun.objects.get(trigger="pre-restore")
        self.assertEqual((pre.status, pre.file_name in backups_now), ("success", True))
        # Undo: restore the safety copy -> back to the changed data
        self.client.force_login(get_user_model().objects.get(username="admin"))
        safety = list_safety_copies()[0][0]
        self.client.post(reverse("backups:restore"), {"safety": safety, "password": "pw123456", "confirm": "RESTORE"})
        self.assertTrue(maintenance.read_status()["ok"])
        self.assertEqual(Employee.objects.get(device_user_id="1").name, "Changed Later")
        self.assertTrue(Employee.objects.filter(device_user_id="2").exists())

    def test_wrong_passphrase_changes_nothing(self):
        from . import maintenance
        self.restore(passphrase="wrong-passphrase")
        status = maintenance.read_status()
        self.assertFalse(status["ok"])
        self.assertIn("not changed", status["message"])
        self.assertFalse(maintenance.is_active())
        self.assertEqual(Employee.objects.get(device_user_id="1").name, "Changed Later")

    def test_guards(self):
        resp = self.restore(password="wrong")
        self.assertContains(resp, "admin password is not correct")
        resp = self.restore(confirm="yes")
        self.assertContains(resp, "Type RESTORE")
        self.assertEqual(Employee.objects.get(device_user_id="1").name, "Changed Later")
        self.assertEqual(self.client.get(reverse("backups:restore"), {"dest": self.dest.pk, "file": "../../etc/passwd"}).status_code, 404)
        staff = get_user_model().objects.create_user("hr", password="pw123456", is_staff=True)
        self.client.force_login(staff)
        self.assertEqual(self.client.get(reverse("backups:restore"), {"dest": self.dest.pk, "file": self.file}).status_code, 403)

    def test_upload_and_restore(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        from . import maintenance
        with open(os.path.join(self.store, self.file), "rb") as fh:
            upload = SimpleUploadedFile(self.file, fh.read())
        resp = self.client.post(reverse("backups:restore_upload"), {"backup_file": upload})
        self.assertEqual(resp.status_code, 302)
        name = resp["Location"].split("upload=")[1]
        self.client.post(reverse("backups:restore"), {"upload": name, "password": "pw123456", "confirm": "RESTORE"})
        self.assertTrue(maintenance.read_status()["ok"], maintenance.read_status())
        self.assertEqual(Employee.objects.get(device_user_id="1").name, "Original Name")
        bad = SimpleUploadedFile("notes.txt", b"hello")
        self.client.post(reverse("backups:restore_upload"), {"backup_file": bad})
        from .restore import uploads_dir
        self.assertEqual(os.listdir(uploads_dir()), [])

    def test_maintenance_screen_and_stale_flag(self):
        from . import maintenance
        maintenance.enter(stage="replace", message="Replacing the database…", label="x", pid=os.getpid())
        with self.assertNoLogs("django.request", level="ERROR"):
            resp = self.client.get(reverse("dashboard"))
        self.assertEqual(resp.status_code, 503)
        self.assertContains(resp, "Restoring the database", status_code=503)
        status = self.client.get("/backups/restore/status.json").json()
        self.assertTrue(status["active"])
        self.assertEqual(status["stage"], "replace")
        maintenance.leave()
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)
        # dead restore process + no progress for a while -> unlocked automatically
        maintenance.enter(stage="download", label="x", pid=999999)
        maintenance.write_status(updated=0)
        import json as _json
        data = maintenance.read_status()
        data["updated"] = 0
        with open(maintenance.status_path(), "w") as fh:
            _json.dump(data, fh)
        self.assertFalse(maintenance.is_active())
        self.assertIn("Nothing was changed", maintenance.read_status()["message"])


class PassphraseGuardTests(TempDirMixin, TestCase):
    def test_backup_now_refused_without_passphrase(self):
        admin = get_user_model().objects.create_superuser("a", "", "Admin-pass-1")
        self.client.force_login(admin)
        BackupDestination.objects.create(name="Local", kind="local", path=self.store)
        page = self.client.get(reverse("backups:index"))
        self.assertContains(page, "Set a backup passphrase before the first backup")
        resp = self.client.post(reverse("backups:now"))
        self.assertRedirects(resp, reverse("backups:index") + "#schedule", fetch_redirect_response=False)
        self.assertFalse(BackupRun.objects.exists())   # no failed run is created
        cfg = BackupSettings.get()
        cfg.passphrase = "Good-passphrase-1"
        cfg.save()
        self.assertNotContains(self.client.get(reverse("backups:index")), "Set a backup passphrase before")
        resp = self.client.post(reverse("backups:now"))
        self.assertEqual(BackupRun.objects.count(), 1)
