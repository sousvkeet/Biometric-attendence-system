"""Tests for desktop/mac_desktop.py (the macOS parts of the desktop app); they run on any system."""
import os
import plistlib
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "desktop"))
import mac_desktop as mac  # noqa: E402


class MacDesktopTests(SimpleTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = mock.patch.object(mac.Path, "home", return_value=Path(self.tmp.name))
        self.home.start()

    def tearDown(self):
        self.home.stop()
        self.tmp.cleanup()

    def test_launch_agent_starts_the_service_at_login(self):
        exe = "/Applications/BioAttend.app/Contents/MacOS/BioAttend"
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(sys, "executable", exe):
            mac.set_autostart(True)
        agent = plistlib.loads(mac.launch_agent_file().read_bytes())
        self.assertEqual(agent["Label"], "com.bioattend.app")
        self.assertEqual(agent["ProgramArguments"], [exe, "--background"])
        self.assertTrue(agent["RunAtLoad"])
        self.assertNotIn("KeepAlive", agent)  # "Quit completely" must stay quit
        self.assertTrue(mac.autostart_enabled())
        mac.set_autostart(False)
        self.assertFalse(mac.autostart_enabled())

    @unittest.skipIf(os.name == "nt", "macOS paths (Windows adds drive letters and backslashes)")
    def test_no_launch_agent_from_the_disk_image(self):
        exe = "/Volumes/BioAttend 1.0.5/BioAttend.app/Contents/MacOS/BioAttend"
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(sys, "executable", exe):
            self.assertTrue(mac.running_from_disk_image())
            mac.set_autostart(True)
        self.assertFalse(mac.autostart_enabled())

    @unittest.skipIf(os.name == "nt", "macOS paths (Windows adds drive letters and backslashes)")
    def test_bundle_path(self):
        exe = "/Applications/BioAttend.app/Contents/MacOS/BioAttend"
        with mock.patch.object(sys, "frozen", True, create=True), mock.patch.object(sys, "executable", exe), \
                mock.patch.object(mac.Path, "resolve", lambda p: p):
            self.assertEqual(str(mac.bundle_path()), "/Applications/BioAttend.app")

    def test_dialog_texts_are_arguments_not_script(self):
        with mock.patch.object(mac.subprocess, "run") as run:
            run.return_value = mock.Mock(returncode=0, stdout="")
            mac.message_box('Quote " and \\ backslash; do shell script "rm -rf ~"')
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[-3:], ["--", 'Quote " and \\ backslash; do shell script "rm -rf ~"', "BioAttend"])
        self.assertFalse(any("rm -rf" in part for part in cmd[:-3]))

    def test_copy_password_button_copies(self):
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return mock.Mock(returncode=0, stdout="Copy password\n" if cmd[0] == "osascript" else "")
        with mock.patch.object(mac.subprocess, "run", side_effect=fake_run):
            mac.show_password_dialog("admin", "s3cret-PW")
        pbcopy = [kw for cmd, kw in calls if cmd == ["pbcopy"]]
        self.assertEqual(pbcopy[0]["input"], "s3cret-PW")
