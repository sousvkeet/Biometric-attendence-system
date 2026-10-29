from io import StringIO

from django.core.management import CommandError, call_command
from django.test import TestCase

from attendance.models import AttendanceLog
from devices.models import Device
from employees.models import Employee


class DemoDataTests(TestCase):
    def test_fills_an_empty_database_with_simulators_only(self):
        call_command("demo_data", days=7, stdout=StringIO())
        self.assertEqual(Employee.objects.count(), 12)
        self.assertEqual(set(Device.objects.values_list("driver", flat=True)), {Device.DRIVER_SIMULATOR})
        self.assertTrue(AttendanceLog.objects.exists())

    def test_refuses_a_database_with_data(self):
        Employee.objects.create(device_user_id="1", name="Real person")
        with self.assertRaises(CommandError):
            call_command("demo_data")
        self.assertEqual(Employee.objects.count(), 1)
