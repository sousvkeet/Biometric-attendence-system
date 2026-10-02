"""Restoring a backup from the web interface (runs in its own process: `manage.py restore_backup --ui`)."""
import json
import os
import shutil
import subprocess
import tempfile
import time

from django.conf import settings
from django.core.management import call_command
from django.db import close_old_connections
from django.utils import timezone

from . import maintenance
from .engine import (BackupError, prepare_restore_file, restore_postgres, restore_sqlite,
                     run_backup, safety_dir)
from .models import BackupDestination, BackupRun

PASSPHRASE_ENV = "BIOATTEND_RESTORE_PASSPHRASE"
STEPS = [("waiting", "Pausing the portal"), ("backup", "Backing up the current data"),
         ("download", "Getting the backup file"), ("verify", "Decrypting and checking"),
         ("replace", "Replacing the database"), ("migrate", "Updating the database structure"),
         ("finish", "Finishing")]
SETTLE_SECONDS = 6  # time for the worker and live listeners to notice maintenance mode
UPLOAD_EXTENSIONS = (".sqlite3.gz", ".sqlite3.gz.enc", ".pgdump", ".pgdump.enc", ".sqlite3")


def job_path():
    return os.path.join(settings.BACKUP_WORK_DIR, "restore-job.json")


def uploads_dir():
    path = os.path.join(settings.BACKUP_WORK_DIR, "uploads")
    os.makedirs(path, exist_ok=True)
    return path


def start(job, passphrase):
    """Enter maintenance mode and launch the restore process. `job`: {source, dest, file, label, backup_first, user}."""
    if maintenance.is_active():
        raise BackupError("A restore is already running.")
    with open(job_path(), "w") as fh:
        json.dump(job, fh)
    os.chmod(job_path(), 0o600)
    maintenance.enter(stage="waiting", message="Starting…", label=job["label"], user=job.get("user", ""),
                      backup_first=job.get("backup_first", False), pid=None)
    try:
        launch(passphrase)
    except Exception:
        maintenance.write_status(finished=True, ok=False, stage="failed", message="Could not start the restore process.")
        maintenance.leave()
        raise


def launch(passphrase):
    env = dict(os.environ)
    env[PASSPHRASE_ENV] = passphrase or ""
    log = open(os.path.join(settings.BACKUP_WORK_DIR, "restore.log"), "a")
    from core.osutil import background_process_kwargs, manage_command
    proc = subprocess.Popen(manage_command("restore_backup", "--ui"), cwd=str(settings.BASE_DIR), env=env,
                            stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                            **background_process_kwargs())
    maintenance.write_status(pid=proc.pid)


def _step(stage, message=None):
    label = dict(STEPS)[stage]
    maintenance.write_status(stage=stage, message=message or label + "…", pid=os.getpid())


def _wait_for_idle(timeout=180):
    from devices.models import DeviceJob
    deadline = time.monotonic() + timeout
    time.sleep(SETTLE_SECONDS)
    while time.monotonic() < deadline:
        close_old_connections()
        if not DeviceJob.objects.filter(status="running").exists() and \
                not BackupRun.objects.filter(status="running").exists():
            return
        _step("waiting", "Waiting for a running device task to finish…")
        time.sleep(3)
    raise BackupError("A device task is still running after 3 minutes; try again later. Nothing was changed.")


def run_restore_job(passphrase=None):
    """Executed by the restore process. Always leaves maintenance mode."""
    with open(job_path()) as fh:
        job = json.load(fh)
    os.remove(job_path())
    passphrase = passphrase if passphrase is not None else os.environ.get(PASSPHRASE_ENV, "")
    from .models import BackupSettings
    passphrase = passphrase or BackupSettings.get().passphrase
    work = os.path.join(settings.BACKUP_WORK_DIR, "tmp")
    os.makedirs(work, exist_ok=True)
    workdir = tempfile.mkdtemp(dir=work)
    replaced = False
    log_lines = []

    def log(msg):
        log_lines.append(f"{timezone.localtime():%H:%M:%S} {msg}")
        maintenance.write_status(log=log_lines[-30:])

    try:
        _step("waiting")
        _wait_for_idle()
        if job.get("backup_first"):
            _step("backup")
            run = BackupRun.objects.create(trigger="pre-restore", status="running", started_at=timezone.now())
            run_backup(run)
            if run.status == "failed":
                raise BackupError("The backup of the current data failed, so nothing was changed. See Backups → "
                                  "History, or untick “Back up the current data first”.")
            log(f"Current data backed up ({run.file_name})")
            pre_backup = {f: getattr(run, f) for f in ("file_name", "size", "sha256", "encrypted", "results", "log")}
            pre_backup.update(started_at=run.started_at, finished_at=run.finished_at)
            job["pre_backup"] = pre_backup
        _step("download")
        path = _fetch(job, workdir, log)
        _step("verify")
        kind, prepared = prepare_restore_file(path, passphrase, workdir, log)
        engine = settings.DATABASES["default"]["ENGINE"]
        if (kind == "sqlite") != ("sqlite3" in engine):
            raise BackupError("This backup is from a different database type than the portal uses.")
        from .logins import capture_logins, is_nologins_backup, reapply_logins
        keep_logins = job.get("keep_logins", True) or is_nologins_backup(job["file"])
        saved_logins = capture_logins() if keep_logins else None
        _step("replace")
        maintenance.write_status(replaced=True)
        replaced = True
        (restore_sqlite if kind == "sqlite" else restore_postgres)(prepared, log)
        _step("migrate")
        close_old_connections()
        call_command("migrate", interactive=False, verbosity=0)
        _step("finish")
        if saved_logins is not None:
            reapply_logins(saved_logins, log)
        _after_restore(job, log)
        if keep_logins:
            message = f"Restored {job['label']}. User accounts and passwords were kept – you are still signed in."
        else:
            message = (f"Restored {job['label']}. Sign in again with the password that was valid when that backup "
                       "was made.")
        maintenance.write_status(finished=True, ok=True, stage="done", logins_kept=keep_logins, message=message,
                                 finished_at=time.time())
    except Exception as exc:
        message = str(exc) if isinstance(exc, BackupError) else f"Unexpected error: {exc}"
        if replaced:
            message += " The database may be incomplete: use Backups → Undo (safety copy) to go back."
        else:
            message += " Your data was not changed."
        maintenance.write_status(finished=True, ok=False, stage="failed", message=message, finished_at=time.time())
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        if job.get("source") == "upload":
            try:
                os.remove(os.path.join(uploads_dir(), job["file"]))
            except OSError:
                pass
        maintenance.leave()


def _fetch(job, workdir, log):
    source, name = job["source"], job["file"]
    if source == "destination":
        from .transports import transport_for
        dest = BackupDestination.objects.get(pk=job["dest"])
        local = os.path.join(workdir, name)
        log(f"Downloading {name} from {dest.name}")
        transport_for(dest).download(name, local)
        return local
    folder = uploads_dir() if source == "upload" else safety_dir()
    path = os.path.join(folder, os.path.basename(name))
    if not os.path.isfile(path):
        raise BackupError("The backup file is no longer there.")
    return path


def _after_restore(job, log):
    from core.models import ActivityLog
    from devices.jobs import enqueue
    from devices.models import DeviceJob
    close_old_connections()
    DeviceJob.objects.filter(status="running").update(status="failed", progress="Interrupted by restore")
    BackupRun.objects.filter(status__in=["running", "pending"]).update(status="failed")
    # Punches made after the backup are still on the devices: download everything again.
    enqueue(DeviceJob.TYPE_PULL_ATTENDANCE, payload={"force": True}, automatic=True)
    if job.get("pre_backup"):  # its record lived in the database that was just replaced
        BackupRun.objects.create(trigger="pre-restore", status="success", **job["pre_backup"])
    ActivityLog.objects.create(action="Database restored", target=job["label"][:200],
                               details=f"Restored from the web by {job.get('user', '')}")
    log("Queued a full attendance download to recover punches made after the backup")

