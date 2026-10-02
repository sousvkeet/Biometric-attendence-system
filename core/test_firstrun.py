import os
import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

from attendance.models import LeaveType, Shift
from core.firstrun import get_setup_code
from core.models import SystemSetting

U = get_user_model()
GOOD = {"company_name": "Example Co", "username": "owner", "email": "owner@example.com",
        "password1": "Strong-pass-2026", "password2": "Strong-pass-2026"}


class FirstRunTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.o = override_settings(VAR_DIR=__import__("pathlib").Path(self.tmp))
        self.o.enable()

    def tearDown(self):
        self.o.disable()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_everything_leads_to_setup_until_admin_exists(self):
        for url in ("/", "/dashboard/", "/login/", "/employees/"):
            resp = self.client.get(url)
            self.assertRedirects(resp, reverse("first_run"), fetch_redirect_response=False, msg_prefix=url)
        self.assertEqual(self.client.get("/healthz").status_code, 200)
        page = self.client.get(reverse("first_run"))
        self.assertContains(page, "Setup code")
        self.assertContains(page, "Administrator email")

    def test_server_needs_setup_code(self):
        resp = self.client.post(reverse("first_run"), GOOD)
        self.assertContains(resp, "This field is required")
        resp = self.client.post(reverse("first_run"), {**GOOD, "setup_code": "WRONG-CODE"})
        self.assertContains(resp, "setup code is not correct")
        self.assertFalse(U.objects.exists())
        code = get_setup_code()
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "setup-code.txt")))
        resp = self.client.post(reverse("first_run"), {**GOOD, "setup_code": code.lower()})
        self.assertRedirects(resp, reverse("dashboard"), fetch_redirect_response=False)
        admin = U.objects.get(username="owner")
        self.assertTrue(admin.is_superuser and admin.is_staff)
        self.assertEqual(admin.email, "owner@example.com")
        self.assertTrue(admin.check_password("Strong-pass-2026"))
        self.assertEqual(SystemSetting.get().company_name, "Example Co")
        self.assertTrue(Shift.objects.exists() and LeaveType.objects.count() == 4)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "setup-code.txt")))  # one-time code removed
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 200)  # signed in
        self.client.logout()
        self.assertRedirects(self.client.get(reverse("first_run")), reverse("home"), fetch_redirect_response=False)
        self.assertEqual(self.client.get("/login/").status_code, 200)

    @override_settings(DESKTOP_MODE=True)
    def test_desktop_app_from_same_pc_needs_no_code(self):
        page = self.client.get(reverse("first_run"))
        self.assertNotContains(page, "Setup code")
        resp = self.client.post(reverse("first_run"), GOOD)
        self.assertRedirects(resp, reverse("dashboard"), fetch_redirect_response=False)
        self.assertTrue(U.objects.filter(username="owner", is_superuser=True).exists())

    @override_settings(DESKTOP_MODE=True)
    def test_desktop_app_from_network_still_needs_code(self):
        page = self.client.get(reverse("first_run"), REMOTE_ADDR="192.168.1.77")
        self.assertContains(page, "Setup code")

    def test_validation(self):
        code = get_setup_code()
        cases = [({"password1": "12345678", "password2": "12345678"}, "entirely numeric"),
                 ({"password2": "Different-pass-1"}, "do not match"),
                 ({"email": "not-an-email"}, "valid email"),
                 ({"username": "bad name!"}, "Enter a valid username")]
        for change, message in cases:
            resp = self.client.post(reverse("first_run"), {**GOOD, "setup_code": code, **change})
            self.assertContains(resp, message, msg_prefix=str(change))
        self.assertFalse(U.objects.exists())

    def test_setup_code_command(self):
        from io import StringIO

        from django.core.management import call_command
        out = StringIO()
        call_command("setup_code", stdout=out)
        self.assertIn(get_setup_code(), out.getvalue())
