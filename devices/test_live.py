import datetime as dt
import shutil
import tempfile
import time
from struct import pack

from django.contrib.auth import get_user_model
from django.test import TestCase, TransactionTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from attendance.models import AttendanceLog, Shift
from core.models import SystemSetting
from employees.models import Employee

from .drivers import DevPunch, SimulatorSession
from .live import DeviceListener, parse_attlog, save_live_punch
from .models import Device, DeviceLiveStatus
from .sync import save_punches


def _quiet(check):
    """The in-memory test database is shared with the listener thread and can be briefly locked."""
    from django.db import OperationalError
    try:
        return check()
    except OperationalError:
        return False


class ParseTests(TestCase):
    def test_documented_32_byte_record(self):
        # payload of the realtime attendance example in the ZK protocol documentation
        data = b"999111333".ljust(24, b"\0") + bytes([1, 0]) + bytes([0x12, 0x06, 0x19, 0x11, 0x29, 0x05])
        self.assertEqual(parse_attlog(data), [("999111333", dt.datetime(2018, 6, 25, 17, 41, 5), 1, 0)])

    def test_other_layouts(self):
        t = bytes([26, 10, 1, 9, 41, 7])
        self.assertEqual(parse_attlog(pack("<IBB", 901, 1, 0) + t), [("901", dt.datetime(2026, 10, 1, 9, 41, 7), 1, 0)])
        rec52 = b"901".ljust(24, b"\0") + bytes([15, 1]) + t + b"\0" * 20
        self.assertEqual(parse_attlog(rec52), [("901", dt.datetime(2026, 10, 1, 9, 41, 7), 15, 1)])
        self.assertEqual(len(parse_attlog(rec52 * 2)), 2)

    def test_garbage_is_ignored(self):
        self.assertEqual(parse_attlog(b"\0" * 32), [])  # invalid date


class LiveStorageTests(TestCase):
    def setUp(self):
        self.device = Device.objects.create(name="906", ip_address="x")
        self.emp = Employee.objects.create(device_user_id="901", name="Test Person")

    def test_live_punch_then_download_has_no_duplicate(self):
        when = dt.datetime(2026, 10, 1, 9, 41, 7)
        log = save_live_punch(self.device, "901", when, 1, 0)
        self.assertEqual(log.employee, self.emp)
        self.assertIsNone(save_live_punch(self.device, "901", when, 1, 0))
        self.assertEqual(save_punches(self.device, [DevPunch("901", when, 1, 0)]), 0)
        self.assertEqual(AttendanceLog.objects.count(), 1)


class LiveFeedTests(TestCase):
    def setUp(self):
        shift = Shift.objects.create(name="Day", start_time=dt.time(9), end_time=dt.time(18), weekly_offs="")
        s = SystemSetting.get()
        s.default_shift = shift
        s.save()
        self.client.force_login(get_user_model().objects.create_superuser("a", "", "pw123456"))
        self.device = Device.objects.create(name="906", ip_address="x")
        self.emp = Employee.objects.create(device_user_id="1", name="Asha Roy")
        self.today = timezone.localdate()

    def punch(self, h, m):
        return AttendanceLog.objects.create(
            employee=self.emp, device=self.device, device_user_id="1",
            punch_time=timezone.make_aware(dt.datetime.combine(self.today, dt.time(h, m))))

    def test_feed_labels_and_cursor(self):
        self.punch(9, 40)
        data = self.client.get(reverse("live_feed")).json()
        self.assertEqual(len(data["punches"]), 1)
        first = data["punches"][0]
        self.assertEqual((first["kind"], first["late"], first["late_minutes"], first["initials"]), ("in", True, 40, "AR"))
        self.assertEqual(data["counts"]["present"], 1)
        again = self.client.get(reverse("live_feed"), {"after": data["cursor"]}).json()
        self.assertEqual(again["punches"], [])
        self.punch(18, 5)
        new = self.client.get(reverse("live_feed"), {"after": data["cursor"]}).json()
        self.assertEqual([p["kind"] for p in new["punches"]], ["out"])

    def test_old_backlog_not_in_today_feed(self):
        AttendanceLog.objects.create(employee=self.emp, device=self.device, device_user_id="1",
                                     punch_time=timezone.now() - dt.timedelta(days=3))
        self.assertEqual(self.client.get(reverse("live_feed")).json()["punches"], [])

    def test_pages(self):
        self.assertContains(self.client.get(reverse("dashboard")), "data-live-feed")
        self.assertContains(self.client.get(reverse("live_monitor")), "monitorClock")


class SimulatorListenerTests(TransactionTestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.override = override_settings(SIMULATOR_DIR=self.tmp)
        self.override.enable()

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_listener_stores_punch_within_seconds(self):
        import threading
        device = Device.objects.create(name="SIM", ip_address="sim", driver=Device.DRIVER_SIMULATOR)
        Employee.objects.create(device_user_id="7", name="Live Tester")
        SimulatorSession(device).open()
        stop = threading.Event()
        listener = DeviceListener(device.pk, stop)
        listener.start()
        try:
            for _ in range(50):
                if _quiet(lambda: DeviceLiveStatus.objects.filter(device=device, status="connected").exists()):
                    break
                time.sleep(0.1)
            SimulatorSession(device).open().add_punch("7")
            for _ in range(80):
                if _quiet(lambda: AttendanceLog.objects.filter(device=device, device_user_id="7").exists()):
                    break
                time.sleep(0.1)
            log = AttendanceLog.objects.get(device=device, device_user_id="7")
            self.assertEqual(log.employee.name, "Live Tester")
            self.assertIsNotNone(DeviceLiveStatus.objects.get(device=device).last_event_at)
        finally:
            stop.set()
            listener.join(5)
