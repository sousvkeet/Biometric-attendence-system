import gzip
import os
import shutil
import sqlite3
import tempfile
import time
from io import StringIO
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.test import TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from attendance.models import AttendanceLog
from employees.models import Employee

from .engine import BackupError, backup_now, decrypt_file
from .models import BackupDestination, BackupRun, BackupSettings
from .transports import BACKUP_NAME_RE

U = get_user_model()


@override_settings()
class LoginDetailsTests(TransactionTestCase):
    def setUp(self):
        if connection.vendor != "sqlite":
            self.skipTest("login exclusion is SQLite-only")
        self.tmp = tempfile.mkdtemp()
        self.o = override_settings(BACKUP_WORK_DIR=os.path.join(self.tmp, "work"))
        self.o.enable()
        from . import restore
        self.patches = [mock.patch.object(restore, "SETTLE_SECONDS", 0),
                        mock.patch.object(restore, "launch", lambda passphrase: restore.run_restore_job(passphrase))]
        for p in self.patches:
            p.start()
        self.store = os.path.join(self.tmp, "store")
        BackupDestination.objects.create(name="Local", kind="local", path=self.store)
        cfg = BackupSettings.get()
        cfg.encrypt, cfg.passphrase = True, "Pass-phrase-123"
        cfg.save()
        self.admin = U.objects.create_superuser("owner", "owner@example.com", "Old-pass-2026")
        self.hr = U.objects.create_user("hr", password="Hr-old-pass-1", is_staff=True)
        emp_user = U.objects.create_user("asha", password="Asha-pass-123")
        self.emp = Employee.objects.create(device_user_id="1", name="Asha", user=emp_user)
        AttendanceLog.objects.create(employee=self.emp, device_user_id="1", source="manual",
                                     punch_time=timezone.now(), created_by=self.hr, note="manual")

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.o.disable()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def open_backup(self, run):
        enc = os.path.join(self.store, run.file_name)
        plain = os.path.join(self.tmp, "plain.gz")
        decrypt_file(enc, plain, "Pass-phrase-123")
        db = os.path.join(self.tmp, "restored.sqlite3")
        with gzip.open(plain) as fin, open(db, "wb") as fout:
            shutil.copyfileobj(fin, fout)
        return db

    def test_backup_without_logins_contains_no_login_details(self):
        self.client.force_login(self.admin)  # creates a session
        run = backup_now(include_logins=False)
        self.assertEqual(run.status, "success", run.log)
        self.assertIn("-nologins.", run.file_name)
        self.assertTrue(BACKUP_NAME_RE.match(run.file_name))
        db = self.open_backup(run)
        con = sqlite3.connect(db)
        self.assertEqual(con.execute("select count(*) from auth_user").fetchone()[0], 0)
        self.assertEqual(con.execute("select count(*) from django_session").fetchone()[0], 0)
        self.assertEqual(con.execute("select count(*) from employees_employee where user_id is not null").fetchone()[0], 0)
        self.assertEqual(con.execute("select count(*) from employees_employee").fetchone()[0], 1)   # data kept
        self.assertEqual(con.execute("select count(*) from attendance_attendancelog").fetchone()[0], 1)
        con.close()
        raw = open(db, "rb").read()
        self.assertNotIn(self.admin.password.encode(), raw)  # hash really gone (VACUUM)
        self.assertNotIn(b"pbkdf2_sha256$", raw)

    def test_backup_with_logins_is_complete(self):
        run = backup_now(include_logins=True)
        self.assertNotIn("nologins", run.file_name)
        con = sqlite3.connect(self.open_backup(run))
        self.assertEqual(con.execute("select count(*) from auth_user").fetchone()[0], 3)
        con.close()

    def _restore(self, run, **extra):
        self.client.force_login(self.admin)
        data = {"dest": 1, "file": run.file_name, "password": "New-pass-2026", "confirm": "RESTORE", **extra}
        dest = BackupDestination.objects.get()
        data["dest"] = dest.pk
        self.client.post(reverse("backups:restore"), data)
        from . import maintenance
        return maintenance.read_status()

    def test_restore_nologins_backup_keeps_current_logins_and_session(self):
        run = backup_now(include_logins=False)
        time.sleep(1.1)
        # after the backup: passwords change, a new user appears, data changes
        self.admin.set_password("New-pass-2026"); self.admin.save()
        U.objects.create_user("newbie", password="Newbie-pass-1")
        Employee.objects.filter(pk=self.emp.pk).update(name="Changed")
        status = self._restore(run)
        self.assertTrue(status["ok"], status)
        self.assertTrue(status["logins_kept"])
        self.assertEqual(Employee.objects.get(device_user_id="1").name, "Asha")             # data restored
        self.assertTrue(U.objects.get(username="owner").check_password("New-pass-2026"))    # today's password
        self.assertTrue(U.objects.filter(username="newbie").exists())
        self.assertEqual(Employee.objects.get(device_user_id="1").user.username, "asha")    # login re-linked
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)            # still signed in

    def test_restore_full_backup_keep_or_bring_back_logins(self):
        run = backup_now(include_logins=True)
        time.sleep(1.1)
        self.admin.set_password("New-pass-2026"); self.admin.save()
        status = self._restore(run, keep_logins="on")
        self.assertTrue(status["ok"] and status["logins_kept"], status)
        owner = U.objects.get(username="owner")
        self.assertTrue(owner.check_password("New-pass-2026"))
        log = AttendanceLog.objects.get(note="manual")
        self.assertEqual(log.created_by.username, "hr")   # "created by" matched to the same user name
        # without keeping: the backup's accounts and passwords come back
        status = self._restore(run)  # keep_logins unticked
        self.assertTrue(status["ok"] and not status["logins_kept"], status)
        self.assertTrue(U.objects.get(username="owner").check_password("Old-pass-2026"))

    def test_backup_now_guards_and_command(self):
        cfg = BackupSettings.get()
        cfg.passphrase_encrypted = ""
        cfg.save()
        with self.assertRaisesMessage(BackupError, "passphrase"):
            backup_now()
        cfg.passphrase = "Pass-phrase-123"
        cfg.save()
        BackupRun.objects.create(status="running")
        with self.assertRaisesMessage(BackupError, "already in progress"):
            backup_now()
        BackupRun.objects.all().delete()
        out = StringIO()
        call_command("backup_now", "--without-logins", stdout=out)
        self.assertIn("-nologins.", out.getvalue())
        BackupDestination.objects.all().delete()
        with self.assertRaisesMessage(BackupError, "destination"):
            backup_now()


class RcloneDownloadTests(TransactionTestCase):
    def test_downloads_verified_rclone_once_when_missing(self):
        import hashlib
        import io
        import zipfile
        tmp = tempfile.mkdtemp()
        try:
            buf = io.BytesIO()
            with zipfile.ZipFile(buf, "w") as z:
                z.writestr("rclone-v1.75.1-linux-amd64/rclone", b"#!/bin/sh\necho fake\n")
            payload = buf.getvalue()
            good = hashlib.sha256(payload).hexdigest()

            class R:
                def __init__(self, text=None, content=None):
                    self.text, self.content = text, content

            def fake_get(url, timeout):
                if url.endswith("SHA256SUMS"):
                    return R(text=f"{good}  rclone-v1.75.1-linux-amd64.zip\n")
                return R(content=payload)
            from . import transports
            with override_settings(VAR_DIR=__import__("pathlib").Path(tmp), RCLONE_BINARY="definitely-not-installed-rclone"), \
                    mock.patch("platform.machine", return_value="x86_64"), mock.patch("platform.system", return_value="Linux"), \
                    mock.patch("os.name", "posix"), mock.patch("requests.get", side_effect=fake_get):
                path = transports.rclone_binary()
                self.assertTrue(path.endswith(os.path.join("bin", "rclone")))
                self.assertEqual(open(path, "rb").read(), b"#!/bin/sh\necho fake\n")
                self.assertEqual(transports.rclone_binary(), path)       # second call: no new download
            os.remove(path)
            bad = lambda url, timeout: R(text="0" * 64 + "  rclone-v1.75.1-linux-amd64.zip\n") if url.endswith("SUMS") else R(content=payload)
            with override_settings(VAR_DIR=__import__("pathlib").Path(tmp), RCLONE_BINARY="definitely-not-installed-rclone"), \
                    mock.patch("platform.machine", return_value="x86_64"), mock.patch("platform.system", return_value="Linux"), \
                    mock.patch("os.name", "posix"), mock.patch("requests.get", side_effect=bad):
                from .transports import TransportError
                with self.assertRaisesMessage(TransportError, "checksum"):
                    transports.rclone_binary()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
