"""Fill an EMPTY BioAttend database with made-up demo data (for trying BioAttend, screenshots and training).

    manage.py demo_data            # refuses to run when employees or devices exist
    manage.py demo_data --days 21

Creates departments, a shift, 12 fictional employees, two *simulator* devices (no real hardware is
contacted) and two weeks of punches - including late arrivals, absences and a missing OUT punch.
"""
import datetime as dt
import random

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from attendance.models import AttendanceLog, Shift
from devices.models import Device
from employees.models import Department, Designation, Employee

PEOPLE = [  # fictional names
    ("Aarav Sharma", "Production", "Supervisor"), ("Priya Nair", "Accounts", "Accountant"),
    ("Rohan Mehta", "Production", "Operator"), ("Ananya Iyer", "HR", "HR Executive"),
    ("Vikram Rao", "Production", "Operator"), ("Sneha Kulkarni", "Sales", "Sales Executive"),
    ("Arjun Das", "Maintenance", "Technician"), ("Meera Pillai", "Accounts", "Accounts Assistant"),
    ("Kabir Singh", "Sales", "Area Manager"), ("Ishita Bose", "HR", "Recruiter"),
    ("Dev Malhotra", "Production", "Operator"), ("Nisha Verma", "Maintenance", "Electrician"),
]


class Command(BaseCommand):
    help = "Fill an empty database with fictional demo employees, simulator devices and punches."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=14, help="Days of punches to create (default 14)")

    @transaction.atomic
    def handle(self, *args, days, **options):
        if Employee.objects.exists() or Device.objects.exists():
            raise CommandError("The database already has employees or devices - demo data is only for an empty one.")
        rnd = random.Random(42)  # the same demo every time
        shift = Shift.objects.filter(name="General").first() or Shift.objects.create(
            name="General", start_time=dt.time(9, 0), end_time=dt.time(18, 0))
        now = timezone.localtime()
        gate = Device.objects.create(name="Main Gate", ip_address="sim", driver=Device.DRIVER_SIMULATOR, area="Head Office",
                                     is_enroll_device=True)
        floor = Device.objects.create(name="Factory Floor", ip_address="sim", driver=Device.DRIVER_SIMULATOR, area="Plant 1")
        employees = []
        for number, (name, department, designation) in enumerate(PEOPLE, start=1):
            employees.append(Employee.objects.create(
                device_user_id=str(1000 + number), name=name, shift=shift,
                department=Department.objects.get_or_create(name=department)[0],
                designation=Designation.objects.get_or_create(name=designation)[0],
                join_date=now.date() - dt.timedelta(days=rnd.randint(90, 900)),
            ))

        tz = timezone.get_current_timezone()
        logs = []
        for back in range(days, -1, -1):
            day = now.date() - dt.timedelta(days=back)
            if day.weekday() == 6:  # Sunday off
                continue
            for emp in employees:
                if rnd.random() < 0.06:  # absent
                    continue
                device = floor if emp.department.name in ("Production", "Maintenance") else gate
                late = rnd.random() < 0.15
                check_in = dt.datetime.combine(day, dt.time(9, 0)) + dt.timedelta(
                    minutes=rnd.randint(16, 50) if late else rnd.randint(-20, 10))
                check_out = dt.datetime.combine(day, dt.time(18, 0)) + dt.timedelta(minutes=rnd.randint(-10, 55))
                punches = [(check_in, 0)]
                if back > 0 and not (back == 1 and emp is employees[4]):  # one missing OUT punch yesterday
                    punches.append((check_out, 1))
                for when, state in punches:
                    aware = timezone.make_aware(when, tz)
                    if aware <= now:
                        logs.append(AttendanceLog(employee=emp, device=device, device_user_id=emp.device_user_id,
                                                  punch_time=aware, verify_type=1, punch_state=state))
        AttendanceLog.objects.bulk_create(logs)
        for device, users in ((gate, len(employees)), (floor, 6)):
            records = AttendanceLog.objects.filter(device=device).count()
            Device.objects.filter(pk=device.pk).update(
                status="online", last_seen=now, last_attendance_sync=now, user_count=users, fingerprint_count=users * 2,
                record_count=records, user_capacity=3000, fingerprint_capacity=3000, record_capacity=100000,
                device_model="Simulator", serial_number=f"DEMO{device.pk:04d}", device_time=now, clock_offset_seconds=0)
        self.stdout.write(self.style.SUCCESS(
            f"Demo data: {len(employees)} employees, 2 simulator devices, {len(logs)} punches over {days} days."))
