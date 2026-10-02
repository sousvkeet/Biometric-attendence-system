"""Maintenance mode for restores.

Kept in files (not the database) because the database itself is being replaced. While the flag exists every
web page shows the restore progress screen and the worker, live listeners and backups pause.
"""
import json
import os
import time

from django.conf import settings
from django.utils import timezone

STALE_AFTER = 120  # seconds without progress from a dead restore process


def _path(name):
    os.makedirs(settings.BACKUP_WORK_DIR, exist_ok=True)
    return os.path.join(settings.BACKUP_WORK_DIR, name)


def flag_path():
    return _path("MAINTENANCE")


def status_path():
    return _path("restore-status.json")


def read_status():
    try:
        with open(status_path()) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def write_status(**fields):
    data = read_status()
    data.update(fields, updated=time.time())
    tmp = status_path() + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(data, fh)
    os.replace(tmp, status_path())
    return data


def _pid_alive(pid):
    from core.osutil import pid_alive
    return pid_alive(pid)


def is_active():
    if not os.path.exists(flag_path()):
        return False
    status = read_status()
    stale = time.time() - status.get("updated", 0) > STALE_AFTER
    if stale and status.get("pid") and not _pid_alive(status["pid"]):
        # The restore process died without finishing: unlock the portal.
        write_status(finished=True, ok=False, stage="failed",
                     message="The restore process stopped unexpectedly. "
                             + ("The database may be incomplete – restore the safety copy (Backups → Undo)."
                                if status.get("replaced") else "Nothing was changed."))
        leave()
        return False
    return True


def enter(**fields):
    with open(flag_path(), "w") as fh:
        fh.write(timezone.now().isoformat())
    return write_status(active=True, finished=False, ok=None, started=time.time(), replaced=False, **fields)


def leave():
    try:
        os.remove(flag_path())
    except FileNotFoundError:
        pass
