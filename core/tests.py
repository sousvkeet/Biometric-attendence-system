import datetime as dt

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from attendance.models import AttendanceLog, Shift
from employees.models import Employee

from .models import SystemSetting
from .timefmt import fmt_datetime, fmt_time, reset_cache


def set_format(value):
    s = SystemSetting.get()
    s.time_format = value
    s.save()


class TimeFormatTests(TestCase):
    def tearDown(self):
        reset_cache()

    def test_24_hour_default(self):
        self.assertEqual(fmt_time(dt.time(18, 5)), "18:05")
        self.assertEqual(fmt_time(dt.datetime(2026, 10, 1, 9, 4, 7), seconds=True), "09:04:07")

    def test_12_hour(self):
        set_format("12")
        cases = {dt.time(0, 5): "12:05 AM", dt.time(9, 4): "9:04 AM", dt.time(12, 0): "12:00 PM", dt.time(18, 5): "6:05 PM"}
        for value, expected in cases.items():
            self.assertEqual(fmt_time(value), expected)
        self.assertEqual(fmt_time(dt.datetime(2026, 10, 1, 23, 59, 30), seconds=True), "11:59:30 PM")
        self.assertEqual(fmt_datetime(dt.datetime(2026, 10, 1, 18, 5), "d M Y"), "01 Oct 2026 6:05 PM")
        self.assertEqual(fmt_time(None), "")

    def test_pages_follow_setting(self):
        admin = get_user_model().objects.create_superuser("a", "", "pw123456")
        self.client.force_login(admin)
        shift = Shift.objects.create(name="Day", start_time=dt.time(9), end_time=dt.time(18), weekly_offs="")
        s = SystemSetting.get()
        s.default_shift = shift
        s.auto_shift_detection = False
        s.save()
        emp = Employee.objects.create(device_user_id="1", name="Asha", join_date=timezone.localdate() - dt.timedelta(days=3))
        AttendanceLog.objects.create(employee=emp, device_user_id="1", source="manual",
                                     punch_time=timezone.make_aware(dt.datetime.combine(timezone.localdate(), dt.time(14, 30))))
        page = self.client.get(reverse("attendance:list"))
        self.assertContains(page, "14:30")
        self.assertContains(page, "09:00–18:00")
        set_format("12")
        page = self.client.get(reverse("attendance:list"))
        self.assertContains(page, "2:30 PM")
        self.assertContains(page, "9:00 AM–6:00 PM")
        self.assertNotContains(page, "14:30")
        feed = self.client.get(reverse("live_feed")).json()
        self.assertEqual(feed["punches"][0]["time"], "2:30:00 PM")
        self.assertContains(self.client.get(reverse("shift_list")), "6:00 PM")
        self.assertContains(self.client.get(reverse("live_monitor")), 'data-12h="1"')

    def test_setting_saved_from_settings_page(self):
        admin = get_user_model().objects.create_superuser("a", "", "pw123456")
        self.client.force_login(admin)
        form = self.client.get(reverse("settings")).context["form"]
        data = {k: v for k, v in form.initial.items() if v is not None and not isinstance(v, bool)}
        data.update({k: "on" for k, v in form.initial.items() if v is True})
        data["time_format"] = "12"
        self.client.post(reverse("settings"), data)
        self.assertEqual(SystemSetting.get().time_format, "12")
        self.assertEqual(fmt_time(dt.time(13, 0)), "1:00 PM")


class PerPageTests(TestCase):
    def setUp(self):
        self.client.force_login(get_user_model().objects.create_superuser("a", "", "pw123456"))
        Employee.objects.bulk_create([Employee(device_user_id=str(1000 + i), name=f"Emp {i:03d}") for i in range(60)])

    def rows(self, **params):
        return len(self.client.get(reverse("employees:list"), params).context["page"].object_list)

    def test_default_choice_remembered_and_all(self):
        self.assertEqual(self.rows(), 50)
        page = self.client.get(reverse("employees:list"))
        self.assertContains(page, "per page")
        self.assertContains(page, "Showing <strong>1–50</strong> of <strong>60</strong>")
        self.assertEqual(self.rows(per_page=25), 25)
        self.assertEqual(self.rows(), 25)                 # remembered for this list
        self.assertEqual(self.rows(per_page="all"), 60)
        self.assertEqual(self.rows(per_page="7"), 60)     # invalid value ignored, keeps last choice
        self.assertEqual(len(self.client.get(reverse("devices:jobs")).context["page"].object_list), 0)

    def test_all_is_capped_for_huge_lists(self):
        emp = Employee.objects.first()
        now = timezone.now()
        AttendanceLog.objects.bulk_create([AttendanceLog(employee=emp, device_user_id=emp.device_user_id, source="manual",
                                                         punch_time=now - dt.timedelta(minutes=i)) for i in range(2100)])
        page = self.client.get(reverse("attendance:logs"), {"per_page": "all", "from": "2000-01-01"}).context["page"]
        self.assertEqual(len(page.object_list), 500)
        self.assertFalse(page.allow_all)

    def test_each_list_remembers_its_own_size(self):
        self.rows(per_page=25)
        page = self.client.get(reverse("attendance:list")).context["page"]
        self.assertEqual(page.per_page_value, 100)


class DesktopNavigationAndFaviconTests(TestCase):
    def setUp(self):
        from django.contrib.auth import get_user_model
        self.client.force_login(get_user_model().objects.create_superuser("a", "", "pw123456"))

    def test_back_and_refresh_only_in_the_local_desktop_window(self):
        from django.test import override_settings
        with override_settings(DESKTOP_MODE=True):
            page = self.client.get(reverse("dashboard"), REMOTE_ADDR="127.0.0.1")
            self.assertContains(page, 'data-nav="back"')
            self.assertContains(page, 'data-nav="reload"')
            remote = self.client.get(reverse("dashboard"), REMOTE_ADDR="192.168.1.50")  # another office PC with a normal browser
            self.assertNotContains(remote, 'data-nav="back"')
        self.assertNotContains(self.client.get(reverse("dashboard"), REMOTE_ADDR="127.0.0.1"), 'data-nav="back"')  # server install

    def test_favicon(self):
        page = self.client.get(reverse("dashboard"))
        self.assertContains(page, "img/favicon-32.png")
        self.assertNotContains(page, 'href="data:,"')
        r = self.client.get("/favicon.ico")
        self.assertEqual(r.status_code, 301)
        self.assertTrue(r["Location"].endswith("img/favicon-32.png"))
