from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Back up now to all enabled destinations (no sign-in needed; uses the saved settings and passphrase)."

    def add_arguments(self, parser):
        group = parser.add_mutually_exclusive_group()
        group.add_argument("--with-logins", action="store_true", help="Include user accounts and passwords.")
        group.add_argument("--without-logins", action="store_true", help="Leave login details out.")

    def handle(self, *args, **opts):
        from backups.engine import BackupError, backup_now
        include = True if opts["with_logins"] else (False if opts["without_logins"] else None)
        try:
            run = backup_now(trigger="command", include_logins=include)
        except BackupError as exc:
            raise CommandError(str(exc))
        self.stdout.write(run.log)
        if run.status == "failed":
            raise SystemExit(1)
