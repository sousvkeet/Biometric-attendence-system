from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Print the one-time setup code needed on the first-time setup page (until an administrator exists)."

    def handle(self, *args, **opts):
        from core.firstrun import get_setup_code, setup_needed
        if not setup_needed():
            self.stdout.write("Setup is already complete (an administrator exists).")
            return
        self.stdout.write(f"Setup code: {get_setup_code()}")
        self.stdout.write("Open the portal in a browser and complete the first-time setup page.")
