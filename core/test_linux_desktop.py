"""Tests for desktop/linux_desktop.py (the Linux parts of the desktop app)."""
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "desktop"))
import linux_desktop as linux  # noqa: E402


class LinuxDesktopTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": self.tmp.name + "/config",
                                                "XDG_DATA_HOME": self.tmp.name + "/data"})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_launcher_has_actions_and_quotes_paths(self):
        with mock.patch.dict(os.environ, {"APPIMAGE": "/home/me/My Apps/BioAttend-1.0.5-x86_64.AppImage"}):
            entry = linux.desktop_entry()
        self.assertIn('Exec="/home/me/My Apps/BioAttend-1.0.5-x86_64.AppImage"\n', entry)
        self.assertIn('Exec="/home/me/My Apps/BioAttend-1.0.5-x86_64.AppImage" --backup\n', entry)
        self.assertIn("Actions=backup;reset-password;data-folder;quit;", entry)
        for _, name, _ in linux.ACTIONS:
            self.assertIn(f"Name={name}\n", entry)

    def test_autostart_on_and_off(self):
        self.assertFalse(linux.autostart_enabled())
        linux.set_autostart(True)
        content = linux.autostart_file().read_text()
        self.assertTrue(linux.autostart_enabled())
        self.assertIn(" --background\n", content)
        self.assertIn("NoDisplay=true", content)
        self.assertNotIn("Actions=", content)
        linux.set_autostart(False)
        self.assertFalse(linux.autostart_enabled())

    def test_user_launcher_only_for_frozen_app_without_system_launcher(self):
        self.assertFalse(linux.install_user_launcher())  # running from source
        with mock.patch.object(sys, "frozen", True, create=True), \
                mock.patch.object(linux, "SYSTEM_LAUNCHER", Path(self.tmp.name) / "none.desktop"), \
                mock.patch.object(linux, "run"):
            self.assertTrue(linux.install_user_launcher())
            self.assertFalse(linux.install_user_launcher())  # unchanged: not rewritten
            launcher = Path(self.tmp.name) / "data" / "applications" / "bioattend.desktop"
            self.assertTrue(launcher.exists())
            linux.remove_user_integration()
            self.assertFalse(launcher.exists())

    def test_system_programs_get_the_system_library_path(self):
        with mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": "/tmp/_MEI123", "LD_LIBRARY_PATH_ORIG": "/usr/lib/x"}):
            self.assertEqual(linux.system_env()["LD_LIBRARY_PATH"], "/usr/lib/x")
        with mock.patch.dict(os.environ, {"LD_LIBRARY_PATH": "/tmp/_MEI123"}):
            os.environ.pop("LD_LIBRARY_PATH_ORIG", None)
            self.assertNotIn("LD_LIBRARY_PATH", linux.system_env())

    def test_browser_choice(self):
        with mock.patch.object(linux.shutil, "which", side_effect=lambda n: "/usr/bin/chromium" if n == "chromium" else None):
            self.assertEqual(linux.find_browser(), "/usr/bin/chromium")
        with mock.patch.dict(os.environ, {"BIOATTEND_BROWSER": "brave"}), \
                mock.patch.object(linux.shutil, "which", side_effect=lambda n: f"/opt/{n}"):
            self.assertEqual(linux.find_browser(), "/opt/brave")
        with mock.patch.object(linux.shutil, "which", return_value=None):
            self.assertIsNone(linux.find_browser())
