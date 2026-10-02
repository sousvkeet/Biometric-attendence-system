from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand

from devices.models import Device


class Command(BaseCommand):
    help = "Create default settings, shift and leave types, and (optionally) an admin account and the first device."

    def add_arguments(self, parser):
        parser.add_argument("--admin-user", default="admin")
        parser.add_argument("--admin-password", default=None,
                            help="Create the admin non-interactively (otherwise use the first-time setup page).")
        parser.add_argument("--admin-email", default="")
        parser.add_argument("--company", default=None)
        parser.add_argument("--device-name")
        parser.add_argument("--device-ip")
        parser.add_argument("--device-port", type=int, default=4370)
        parser.add_argument("--device-serial", default="")
        parser.add_argument("--read-only", action="store_true", help="Add the device in write-protected mode.")

    def handle(self, *args, **o):
        from core.firstrun import create_defaults, get_setup_code, setup_needed
        create_defaults(company=o["company"])
        self.stdout.write("Default shift 'General' 09:00-18:00 (Sunday off) and leave types are in place.")

        User = get_user_model()
        if o["admin_password"]:  # unattended installs; normally the first-time setup page is used
            if not User.objects.filter(username=o["admin_user"]).exists():
                User.objects.create_superuser(o["admin_user"], o["admin_email"] or "", o["admin_password"])
                self.stdout.write(self.style.SUCCESS(f"Admin account '{o['admin_user']}' created."))
            else:
                self.stdout.write(f"Admin account '{o['admin_user']}' already exists.")
        elif setup_needed():
            self.stdout.write(self.style.WARNING(
                "No administrator yet: open the portal in a browser to create it on the first-time setup page.\n"
                f"Setup code: {get_setup_code()}"))

        if o["device_ip"]:
            device, created = Device.objects.get_or_create(
                ip_address=o["device_ip"], port=o["device_port"],
                defaults=dict(name=o["device_name"] or o["device_ip"], serial_number=o["device_serial"],
                              read_only=o["read_only"], area="Area1"),
            )
            self.stdout.write(f"Device {device.name} ({device.ip_address}:{device.port}) "
                              f"{'added' if created else 'already present'}; read-only={device.read_only}")
        self.stdout.write(self.style.SUCCESS("Setup complete."))
