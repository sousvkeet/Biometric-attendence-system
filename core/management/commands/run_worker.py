import datetime as dt
import threading

from django.core.management.base import BaseCommand
from django.db import close_old_connections
from django.utils import timezone

from backups import maintenance
from core.models import SystemSetting
from devices.jobs import claim_next_job, enqueue, run_job, run_pending_jobs
from devices.models import DeviceJob


class Command(BaseCommand):
    help = "Background worker: runs device jobs and the automatic attendance / user sync schedule."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true", help="Run queued jobs and exit.")
        parser.add_argument("--no-schedule", action="store_true", help="Do not create automatic sync jobs.")
        parser.add_argument("--no-live", action="store_true", help="Do not run the live punch listeners.")

    def handle(self, *args, **opts):
        if opts["once"]:
            n = run_pending_jobs()
            self.stdout.write(f"Ran {n} job(s).")
            return

        stop = threading.Event()
        try:
            self.serve(stop, live=not opts["no_live"], schedule=not opts["no_schedule"])
        except KeyboardInterrupt:
            self.stdout.write("Stopping worker.")
        finally:
            stop.set()

    def serve(self, stop, live=True, schedule=True):
        """Run until `stop` is set. Used by this command and by the Windows desktop app."""
        self._lock = self._single_instance()
        self._recover_interrupted()
        threading.Thread(target=self._heartbeat, args=(stop,), daemon=True, name="heartbeat").start()
        if live:
            from devices.live import LiveManager
            LiveManager(stop).start()
        from backups.worker import BackupManager
        BackupManager(stop).start()
        self.stdout.write(self.style.SUCCESS("Worker started. Press Ctrl+C to stop."))
        while not stop.is_set():
            if maintenance.is_active():  # a restore is replacing the database
                stop.wait(2)
                continue
            close_old_connections()
            if schedule:
                self._schedule()
            job = claim_next_job()
            if job:
                self.stdout.write(f"Running job #{job.pk} {job.get_job_type_display()} ({job.target_label})")
                run_job(job)
                self.stdout.write(f"  -> {job.status}")
            else:
                stop.wait(1)
        from django.db import connection
        connection.close()
        self._lock.close()

    def _single_instance(self):
        """Only one worker may talk to the devices; a second one exits immediately."""
        from django.conf import settings
        from django.core.management.base import CommandError

        from core.osutil import acquire_exclusive_lock
        run_dir = settings.VAR_DIR / "run"
        run_dir.mkdir(parents=True, exist_ok=True)
        handle = acquire_exclusive_lock(run_dir / "worker.lock")
        if handle is None:
            raise CommandError("Another BioAttend worker is already running (only one is allowed). "
                               "Stop it first, e.g. 'pm2 stop bioattend-worker' or 'systemctl stop bioattend-worker'.")
        return handle

    def _heartbeat(self, stop):
        while not stop.is_set():
            try:
                if maintenance.is_active():
                    stop.wait(5)
                    continue
                close_old_connections()
                SystemSetting.get()
                SystemSetting.objects.filter(pk=1).update(worker_heartbeat=timezone.now())
            except Exception as exc:  # keep beating even if the DB is briefly locked
                self.stderr.write(f"heartbeat error: {exc}")
            stop.wait(15)
        from django.db import connection
        connection.close()

    def _recover_interrupted(self):
        for job in DeviceJob.objects.filter(status="running"):
            job.write("Interrupted (worker restarted).")
            DeviceJob.objects.filter(pk=job.pk).update(status="failed", progress="Interrupted", finished_at=timezone.now())

    def _schedule(self):
        s = SystemSetting.get()
        now = timezone.now()
        if s.auto_attendance_sync and s.attendance_sync_interval:
            if not s.last_auto_attendance_sync or now - s.last_auto_attendance_sync >= dt.timedelta(minutes=s.attendance_sync_interval):
                enqueue(DeviceJob.TYPE_PULL_ATTENDANCE, automatic=True)
                SystemSetting.objects.filter(pk=1).update(last_auto_attendance_sync=now)
        if s.auto_user_sync and s.user_sync_interval:
            if not s.last_auto_user_sync or now - s.last_auto_user_sync >= dt.timedelta(minutes=s.user_sync_interval):
                enqueue(DeviceJob.TYPE_SYNC_USERS, automatic=True)
                SystemSetting.objects.filter(pk=1).update(last_auto_user_sync=now)
        # Housekeeping: keep the job list readable (portal records only).
        if now.minute == 0 and now.second < 5:
            DeviceJob.objects.filter(
                is_automatic=True, status="success", created_at__lt=now - dt.timedelta(days=7)
            ).delete()
