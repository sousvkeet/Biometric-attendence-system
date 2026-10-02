import os
import shutil
import tempfile

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from backups.engine import BackupError, prepare_restore_file, restore_postgres, restore_sqlite
from backups.models import BackupDestination, BackupSettings
from backups.transports import transport_for


class Command(BaseCommand):
    help = ("Restore the database from a backup file. STOP the portal and worker first (Ctrl+C on run.sh / "
            "pm2 stop all). A safety copy of the current database is made before anything is replaced.")

    def add_arguments(self, parser):
        parser.add_argument("file", nargs="?", help="Path to a backup file, or a file name with --destination.")
        parser.add_argument("--destination", help="Name or ID of a backup destination to download from.")
        parser.add_argument("--latest", action="store_true", help="With --destination: use the newest backup.")
        parser.add_argument("--list", action="store_true", help="With --destination: only list available backups.")
        parser.add_argument("--passphrase", help="Passphrase for encrypted backups (default: the saved one).")
        parser.add_argument("--yes", action="store_true", help="Do not ask for confirmation.")
        parser.add_argument("--restore-logins", action="store_true",
                            help="Also bring back the user accounts/passwords stored in the backup "
                                 "(default: keep the current ones).")
        parser.add_argument("--ui", action="store_true", help=argparse_suppress())

    def handle(self, *args, **o):
        if o["ui"]:  # started by the web interface
            from backups.restore import run_restore_job
            run_restore_job()
            return
        workdir = tempfile.mkdtemp(dir=_ensure(os.path.join(settings.BACKUP_WORK_DIR, "tmp")))
        try:
            path = self._get_file(o, workdir)
            if path is None:
                return
            passphrase = o["passphrase"] or BackupSettings.get().passphrase
            kind, prepared = prepare_restore_file(path, passphrase, workdir, self.stdout.write)
            if not o["yes"]:
                answer = input(f"Replace the current database with {os.path.basename(path)}? "
                               "The portal and worker must be stopped. Type YES: ")
                if answer.strip() != "YES":
                    raise CommandError("Cancelled")
            from django.core.management import call_command

            from backups.logins import capture_logins, is_nologins_backup, reapply_logins
            keep = not o["restore_logins"] or is_nologins_backup(os.path.basename(path))
            saved = capture_logins() if keep else None
            if kind == "sqlite":
                restore_sqlite(prepared, self.stdout.write)
            else:
                restore_postgres(prepared, self.stdout.write)
            call_command("migrate", interactive=False, verbosity=0)
            if saved is not None:
                reapply_logins(saved, self.stdout.write)
            self.stdout.write(self.style.SUCCESS("Restore complete. Start the portal again (./run.sh)."))
        except BackupError as exc:
            raise CommandError(str(exc))
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    def _get_file(self, o, workdir):
        if not o["destination"]:
            if not o["file"] or not os.path.isfile(o["file"]):
                raise CommandError("Give a backup file path, or --destination NAME (--latest / --list).")
            return o["file"]
        q = BackupDestination.objects.filter(pk=o["destination"]) if o["destination"].isdigit() else \
            BackupDestination.objects.filter(name=o["destination"])
        dest = q.first()
        if dest is None:
            raise CommandError(f"No destination '{o['destination']}'")
        transport = transport_for(dest)
        names = transport.list_backups()
        if o["list"]:
            for n in names:
                self.stdout.write(n)
            return None
        name = names[-1] if o["latest"] and names else o["file"]
        if not name or name not in names:
            raise CommandError("Backup not found on that destination (use --list).")
        local = os.path.join(workdir, name)
        self.stdout.write(f"Downloading {name} from {dest.name}...")
        transport.download(name, local)
        return local


def argparse_suppress():
    import argparse
    return argparse.SUPPRESS


def _ensure(path):
    os.makedirs(path, exist_ok=True)
    return path
