import logging
import threading

from django.db import close_old_connections

from . import maintenance
from .engine import claim_pending_run, run_backup, schedule_due

logger = logging.getLogger("core")


class BackupManager(threading.Thread):
    """Runs backups in their own thread so uploads never delay device jobs (runs inside run_worker)."""

    def __init__(self, stop):
        super().__init__(daemon=True, name="backups")
        self.stop = stop

    def run(self):
        from django.db import connection
        try:
            self._run()
        finally:
            connection.close()

    def _run(self):
        from .models import BackupRun
        close_old_connections()
        for run in BackupRun.objects.filter(status="running"):  # interrupted by a restart
            run.write("Interrupted (worker restarted).")
            BackupRun.objects.filter(pk=run.pk).update(status="failed")
        while not self.stop.is_set():
            try:
                if maintenance.is_active():
                    self.stop.wait(5)
                    continue
                close_old_connections()
                schedule_due()
                run = claim_pending_run()
                if run:
                    run_backup(run)
                    continue
            except Exception:
                logger.exception("Backup manager error")
            self.stop.wait(5)
