import datetime as dt

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.models import SystemSetting
from employees.models import Employee

from .engine import compute_attendance
from .models import AttendanceLog, Holiday, LeaveRequest, LeaveType, RegularizationRequest, Shift


def punch(emp, d, hh, mm):
    return AttendanceLog.objects.create(
        employee=emp, device_user_id=emp.device_user_id, source="manual",
        punch_time=timezone.make_aware(dt.datetime.combine(d, dt.time(hh, mm))),
    )


class EngineTests(TestCase):
    def setUp(self):
        self.shift = Shift.objects.create(name="Day", start_time=dt.time(9), end_time=dt.time(18), weekly_offs="6")
        s = SystemSetting.get()
        s.default_shift = self.shift
        s.save()
        self.emp = Employee.objects.create(device_user_id="1", name="Alice", join_date=dt.date(2026, 1, 1))
        self.mon = dt.date(2026, 9, 7)  # a Monday

    def day(self, d, today=None):
        return compute_attendance([self.emp], d, d, today=today or dt.date(2026, 12, 31))[self.emp.pk][d]

    def test_present_late_overtime(self):
        punch(self.emp, self.mon, 9, 40)
        punch(self.emp, self.mon, 12, 0)
        punch(self.emp, self.mon, 19, 30)
        r = self.day(self.mon)
        self.assertEqual(r.status, "P")
        self.assertTrue(r.is_late)
        self.assertEqual(r.late_minutes, 40)
        self.assertEqual(r.worked_minutes, 590)
        self.assertEqual(r.overtime_minutes, 50)

    def test_first_punch_present_last_punch_out(self):
        punch(self.emp, self.mon, 9, 5)
        punch(self.emp, self.mon, 13, 0)
        punch(self.emp, self.mon, 17, 30)
        r = self.day(self.mon)
        self.assertEqual((r.status, r.first_in.time(), r.last_out.time()), ("P", dt.time(9, 5), dt.time(17, 30)))
        self.assertFalse(r.out_missing)
        self.assertTrue(r.is_early)

    def test_single_punch_in_office_then_out_missing(self):
        punch(self.emp, self.mon, 9, 0)
        during = compute_attendance([self.emp], self.mon, self.mon, today=self.mon,
                                    now=dt.datetime.combine(self.mon, dt.time(12, 0)))[self.emp.pk][self.mon]
        self.assertEqual(during.status, "P")
        self.assertTrue(during.in_progress)
        self.assertFalse(during.out_missing)
        after = compute_attendance([self.emp], self.mon, self.mon, today=self.mon,
                                   now=dt.datetime.combine(self.mon, dt.time(18, 30)))[self.emp.pk][self.mon]
        self.assertEqual(after.status, "P")  # still present - first punch counts
        self.assertTrue(after.out_missing)
        self.assertEqual(after.expected_out, dt.datetime.combine(self.mon, dt.time(18)))
        self.assertIn("OUT missing", after.label)
        self.assertTrue(self.day(self.mon).out_missing)  # past day

    def test_half_day_short_hours_absent(self):
        punch(self.emp, self.mon, 9, 0)
        punch(self.emp, self.mon, 12, 0)
        self.assertEqual(self.day(self.mon).status, "HD")
        tue = self.mon + dt.timedelta(days=1)
        punch(self.emp, tue, 9, 0)
        punch(self.emp, tue, 15, 0)
        r = self.day(tue)
        self.assertEqual(r.status, "P")
        self.assertTrue(r.short_hours)
        self.assertEqual(self.day(self.mon + dt.timedelta(days=2)).status, "A")

    def test_weekly_off_holiday_leave(self):
        self.assertEqual(self.day(self.mon - dt.timedelta(days=1)).status, "WO")
        Holiday.objects.create(date=self.mon, name="Festival")
        self.assertEqual(self.day(self.mon).status, "H")
        tue = self.mon + dt.timedelta(days=1)
        lt = LeaveType.objects.create(name="Casual", code="CL", annual_quota=12)
        LeaveRequest.objects.create(employee=self.emp, leave_type=lt, start_date=tue, end_date=tue, status="approved")
        r = self.day(tue)
        self.assertEqual(r.status, "L")
        self.assertEqual(r.code, "CL")

    def test_night_shift_crosses_midnight(self):
        night = Shift.objects.create(name="Night", start_time=dt.time(22), end_time=dt.time(6), weekly_offs="")
        self.emp.shift = night
        self.emp.save()
        punch(self.emp, self.mon, 21, 55)
        punch(self.emp, self.mon + dt.timedelta(days=1), 6, 5)
        r = self.day(self.mon)
        self.assertEqual(r.status, "P")
        self.assertEqual(r.worked_minutes, 490)
        self.assertFalse(r.is_late)

    def test_before_join_date_not_counted(self):
        d = dt.date(2025, 12, 30)
        self.assertEqual(self.day(d).status, "")


class PageTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.admin = User.objects.create_superuser("admin", "", "pass12345")
        self.client.force_login(self.admin)
        self.shift = Shift.objects.create(name="Day")
        self.emp = Employee.objects.create(device_user_id="5", name="Eve")
        punch(self.emp, timezone.localdate(), 9, 0)
        from devices.models import Device
        self.device = Device.objects.create(name="D1", ip_address="sim", driver="simulator")

    def test_admin_pages_render(self):
        urls = [
            reverse("dashboard"), reverse("settings"), reverse("activity_log"),
            reverse("employees:list"), reverse("employees:create"), reverse("employees:detail", args=[self.emp.pk]),
            reverse("employees:edit", args=[self.emp.pk]), reverse("employees:login", args=[self.emp.pk]),
            reverse("devices:list"), reverse("devices:create"), reverse("devices:detail", args=[self.device.pk]),
            reverse("devices:edit", args=[self.device.pk]), reverse("devices:sync_center"), reverse("devices:jobs"),
            reverse("attendance:logs"), reverse("attendance:manual_punch"), reverse("attendance:daily"),
            reverse("attendance:list"), reverse("attendance:list") + "?period=last_month&status=late",
            reverse("attendance:monthly"), reverse("attendance:exceptions"),
            reverse("attendance:employee_card", args=[self.emp.pk]), reverse("attendance:leaves"),
            reverse("attendance:leave_create"), reverse("attendance:regularizations"),
            reverse("shift_list"), reverse("shift_create"), reverse("holiday_list"), reverse("leavetype_list"),
            reverse("department_list"), reverse("designation_list"), reverse("password_change"),
        ]
        for url in urls:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200, url)

    def test_exports(self):
        for url in [reverse("attendance:list") + "?export=xlsx", reverse("attendance:list") + "?export=csv",reverse("attendance:daily") + "?export=xlsx", reverse("attendance:monthly") + "?export=xlsx",
                    reverse("attendance:logs") + "?export=csv", reverse("attendance:logs") + "?export=xlsx",
                    reverse("attendance:exceptions") + "?export=xlsx", reverse("employees:list") + "?export=xlsx"]:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200, url)
            self.assertIn("attachment", resp["Content-Disposition"])

    def test_create_employee_queues_push_job(self):
        resp = self.client.post(reverse("employees:create"), {
            "device_user_id": "42", "name": "New  Person", "status": "active", "privilege": 0,
            "sync_to_all_devices": "on", "card_number": "",
        })
        self.assertEqual(resp.status_code, 302, getattr(resp, "context", None) and resp.context["form"].errors)
        emp = Employee.objects.get(device_user_id="42")
        self.assertEqual(emp.name, "New Person")
        from devices.models import DeviceJob
        self.assertTrue(DeviceJob.objects.filter(job_type="push_employee", employee=emp).exists())

    def test_edit_employee_with_legacy_device_id(self):
        emp = Employee.objects.create(device_user_id="10187\n", name="Legacy")
        resp = self.client.post(reverse("employees:edit", args=[emp.pk]), {
            "device_user_id": "x", "name": "Legacy Renamed", "status": "active", "privilege": 0, "card_number": "",
        })
        self.assertEqual(resp.status_code, 302)
        emp.refresh_from_db()
        self.assertEqual((emp.device_user_id, emp.name), ("10187\n", "Legacy Renamed"))
        bad = self.client.post(reverse("employees:create"), {"device_user_id": "12 3", "name": "X", "status": "active", "privilege": 0})
        self.assertEqual(bad.status_code, 200)

    def test_deactivate_stale(self):
        old = Employee.objects.create(device_user_id="77", name="Old")
        AttendanceLog.objects.create(employee=old, device_user_id="77", source="manual",
                                     punch_time=timezone.now() - dt.timedelta(days=200))
        self.client.post(reverse("employees:deactivate_stale"), {"days": 60})
        old.refresh_from_db()
        self.emp.refresh_from_db()
        self.assertEqual(old.status, "inactive")
        self.assertEqual(self.emp.status, "active")

    def test_device_punches_cannot_be_deleted(self):
        log = AttendanceLog.objects.create(employee=self.emp, device=self.device, device_user_id="5",
                                           punch_time=timezone.now(), source="device")
        self.client.post(reverse("attendance:delete_punch", args=[log.pk]))
        self.assertTrue(AttendanceLog.objects.filter(pk=log.pk).exists())

    def test_self_service_flow(self):
        User = get_user_model()
        u = User.objects.create_user("eve", password="pass12345")
        self.emp.user = u
        self.emp.save()
        self.client.logout()
        self.client.force_login(u)
        for name in ("selfservice:home", "selfservice:attendance", "selfservice:leaves", "selfservice:regularizations"):
            self.assertEqual(self.client.get(reverse(name)).status_code, 200, name)
        self.assertEqual(self.client.get(reverse("dashboard")).status_code, 302)  # staff only
        d = timezone.localdate() - dt.timedelta(days=1)
        self.client.post(reverse("selfservice:regularizations"),
                         {"date": d.isoformat(), "time": "18:05", "punch_state": 1, "reason": "Forgot"})
        reg = RegularizationRequest.objects.get(employee=self.emp)
        self.client.force_login(self.admin)
        self.client.post(reverse("attendance:regularization_review", args=[reg.pk]), {"decision": "approve"})
        reg.refresh_from_db()
        self.assertEqual(reg.status, "approved")
        self.assertTrue(AttendanceLog.objects.filter(employee=self.emp, source="regularization").exists())


class AttendancePageTests(TestCase):
    def setUp(self):
        from employees.models import Department
        self.client.force_login(get_user_model().objects.create_superuser("adm", "", "pw123456"))
        shift = Shift.objects.create(name="Day", start_time=dt.time(9), end_time=dt.time(18), weekly_offs="")
        s = SystemSetting.get()
        s.default_shift = shift
        s.save()
        self.sales = Department.objects.create(name="Sales")
        self.ops = Department.objects.create(name="Ops")
        self.today = timezone.localdate()
        self.y = self.today - dt.timedelta(days=1)
        self.a = Employee.objects.create(device_user_id="1", name="Asha", department=self.sales, join_date=self.y)
        self.b = Employee.objects.create(device_user_id="2", name="Bimal", department=self.ops, join_date=self.y)
        self.c = Employee.objects.create(device_user_id="3", name="Chitra", department=self.ops, join_date=self.y)
        punch(self.a, self.y, 9, 30)                      # late, OUT missing
        punch(self.b, self.y, 8, 55)
        punch(self.b, self.y, 18, 10)                     # complete day
        # Chitra: absent yesterday

    def get(self, **params):
        return self.client.get(reverse("attendance:list"), {"period": "yesterday", **params})

    def test_counts_and_rows(self):
        r = self.get()
        counts = r.context["counts"]
        self.assertEqual((counts["present"], counts["absent"], counts["late"], counts["out_missing"]), (2, 1, 1, 1))
        self.assertContains(r, "OUT missing")
        self.assertContains(r, "shift ends 18:00")
        self.assertContains(r, "+ Add OUT")

    def test_filters_chips_and_clear(self):
        r = self.get(status="out_missing", department=self.sales.pk)
        names = [e.name for e, _ in r.context["page"]]
        self.assertEqual(names, ["Asha"])
        chip_labels = [c[0] for c in r.context["chips"]]
        self.assertIn("Department: Sales", chip_labels)
        self.assertIn("Status: OUT missing", chip_labels)
        self.assertIn("Yesterday", chip_labels)
        # removing one chip keeps the others
        remove_status = dict(r.context["chips"])["Status: OUT missing"]
        self.assertNotIn("status=", remove_status)
        self.assertIn("department=", remove_status)
        self.assertContains(r, "Clear all filters")
        # cleared page = today, no filters
        cleared = self.client.get(reverse("attendance:list"))
        self.assertEqual(cleared.context["chips"], [])
        self.assertEqual(cleared.context["date_from"], self.today)

    def test_search_and_custom_range(self):
        r = self.client.get(reverse("attendance:list"), {"from": self.y.isoformat(), "to": self.y.isoformat(), "q": "bim"})
        self.assertEqual([e.name for e, _ in r.context["page"]], ["Bimal"])
        self.assertEqual(r.context["period"], "custom")

    def test_add_out_prefills_manual_punch(self):
        url = reverse("attendance:manual_punch") + f"?employee={self.a.pk}&at={self.y}T18:00&state=1&next=/attendance/"
        form = self.client.get(url).context["form"]
        self.assertEqual(form.initial["punch_state"], 1)
        self.assertEqual(form.initial["note"], "Missed OUT punch")
        resp = self.client.post(url, {"employee": self.a.pk, "punch_time": f"{self.y}T18:00", "punch_state": 1,
                                      "note": "Missed OUT punch", "next": "/attendance/?period=yesterday"})
        self.assertRedirects(resp, "/attendance/?period=yesterday")
        after = self.get().context["counts"]
        self.assertEqual(after["out_missing"], 0)


class AutoShiftTests(TestCase):
    def setUp(self):
        self.general = Shift.objects.create(name="General", start_time=dt.time(9), end_time=dt.time(18), weekly_offs="")
        self.morning = Shift.objects.create(name="Morning", start_time=dt.time(6), end_time=dt.time(14), weekly_offs="")
        self.evening = Shift.objects.create(name="Evening", start_time=dt.time(14), end_time=dt.time(22), weekly_offs="")
        self.night = Shift.objects.create(name="Night", start_time=dt.time(22), end_time=dt.time(6), weekly_offs="")
        s = SystemSetting.get()
        s.default_shift = self.general
        s.auto_shift_detection = True
        s.save()
        self.emp = Employee.objects.create(device_user_id="1", name="Auto", join_date=dt.date(2026, 1, 1))
        self.d = dt.date(2026, 9, 7)

    def day(self, d=None, emp=None):
        d = d or self.d
        emp = emp or self.emp
        return compute_attendance([emp], d, d, today=dt.date(2026, 12, 31))[emp.pk][d]

    def test_each_punch_picks_its_shift(self):
        cases = [((6, 10), (14, 5), "Morning", False), ((9, 20), (18, 0), "General", True),
                 ((13, 50), (22, 10), "Evening", False)]
        for i, (inp, outp, name, late) in enumerate(cases):
            d = self.d + dt.timedelta(days=i)
            punch(self.emp, d, *inp)
            punch(self.emp, d, *outp)
            r = self.day(d)
            self.assertEqual((r.shift_name, r.shift_auto, r.is_late, r.out_missing), (name, True, late, False), name)

    def test_night_shift_across_midnight_is_one_day(self):
        punch(self.emp, self.d, 21, 55)
        punch(self.emp, self.d + dt.timedelta(days=1), 6, 5)
        r = self.day()
        self.assertEqual((r.shift_name, r.worked_minutes, r.is_late), ("Night", 490, False))
        self.assertEqual(r.last_out, dt.datetime.combine(self.d + dt.timedelta(days=1), dt.time(6, 5)))
        nxt = self.day(self.d + dt.timedelta(days=1))
        self.assertEqual(nxt.punches, [])  # the 06:05 OUT does not start a new day

    def test_detection_windows_decide_borderline_punches(self):
        punch(self.emp, self.d, 11, 45)       # within General's window (until 12:00) -> late for General
        r = self.day()
        self.assertEqual((r.shift_name, r.late_minutes), ("General", 165))
        d2 = self.d + dt.timedelta(days=1)
        punch(self.emp, d2, 12, 30)           # Evening window opens 12:00 -> early for Evening
        r2 = self.day(d2)
        self.assertEqual((r2.shift_name, r2.is_late), ("Evening", False))

    def test_out_missing_uses_detected_shift_end(self):
        punch(self.emp, self.d, 6, 0)
        r = compute_attendance([self.emp], self.d, self.d, today=self.d,
                               now=dt.datetime.combine(self.d, dt.time(15, 0)))[self.emp.pk][self.d]
        self.assertEqual(r.shift_name, "Morning")
        self.assertTrue(r.out_missing)  # Morning ended at 14:00

    def test_fixed_shift_and_switch_off(self):
        fixed = Employee.objects.create(device_user_id="2", name="Fixed", shift=self.general, join_date=dt.date(2026, 1, 1))
        punch(fixed, self.d, 13, 50)
        r = self.day(emp=fixed)
        self.assertEqual((r.shift_name, r.shift_auto, r.is_late), ("General", False, True))
        s = SystemSetting.get()
        s.auto_shift_detection = False
        s.save()
        punch(self.emp, self.d, 13, 50)
        self.assertEqual((self.day().shift_name, self.day().shift_auto), ("General", False))

    def test_shift_excluded_from_detection(self):
        self.evening.auto_detect = False
        self.evening.save()
        punch(self.emp, self.d, 13, 50)
        self.assertNotEqual(self.day().shift_name, "Evening")
