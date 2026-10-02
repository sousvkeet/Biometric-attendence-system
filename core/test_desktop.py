"""Tests for the cross-platform helpers and the desktop launcher's configuration (run on any OS)."""
import importlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from core import osutil


class OsUtilTests(SimpleTestCase):
    def test_pid_alive(self):
        self.assertTrue(osutil.pid_alive(os.getpid()))
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        self.assertFalse(osutil.pid_alive(proc.pid))
        self.assertFalse(osutil.pid_alive(None))
        self.assertFalse(osutil.pid_alive("x"))

    def test_exclusive_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = osutil.acquire_exclusive_lock(Path(tmp) / "a.lock")
            self.assertIsNotNone(first)
            code = "import sys; sys.path.insert(0, %r); from core.osutil import acquire_exclusive_lock as a; " \
                   "print(a(%r) is None)" % (os.getcwd(), str(Path(tmp) / "a.lock"))
            out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True).stdout.strip()
            self.assertEqual(out, "True")  # a second process cannot take the lock
            first.close()

    def test_manage_command(self):
        cmd = osutil.manage_command("restore_backup", "--ui")
        self.assertTrue(cmd[1].endswith("manage.py"))
        self.assertEqual(cmd[-2:], ["restore_backup", "--ui"])


class DesktopConfigTests(SimpleTestCase):
    def test_first_run_config_and_environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = dict(os.environ)
            try:
                os.environ["BIOATTEND_HOME"] = tmp
                sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "desktop"))
                desk = importlib.import_module("bioattend_desktop")
                desk = importlib.reload(desk)
                cfg = desk.ensure_config()
                self.assertGreaterEqual(len(cfg["SECRET_KEY"]), 40)
                self.assertEqual(desk.ensure_config()["SECRET_KEY"], cfg["SECRET_KEY"])  # stable across starts
                self.assertEqual(cfg["PORT"], "30009")
                cfg["PORT"] = "8090"  # an install from an earlier version
                desk.write_config(cfg)
                self.assertEqual(desk.ensure_config()["PORT"], "30009")
                lan = desk.configure_environment(cfg)
                self.assertFalse(lan)
                self.assertEqual(os.environ["ALLOWED_HOSTS"], "localhost,127.0.0.1")
                self.assertEqual(os.environ["SQLITE_PATH"], str(Path(tmp) / "bioattend.sqlite3"))
                cfg["LAN_ACCESS"] = "true"
                desk.write_config(cfg)
                self.assertTrue(desk.configure_environment(desk.read_config()))
                self.assertIn("127.0.0.1", os.environ["ALLOWED_HOSTS"].split(","))
            finally:
                os.environ.clear()
                os.environ.update(saved)
                sys.path.pop(0)
