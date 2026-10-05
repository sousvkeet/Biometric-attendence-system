"""Background job queue: the web UI enqueues DeviceJob rows, the worker (`manage.py run_worker`) runs them."""
import logging

from django.utils import timezone

from . import sync
from .drivers import EnrollCancelled
from .models import Device, DeviceJob

logger = logging.getLogger("devices")


def enqueue(job_type, device=None, employee=None, payload=None, user=None, automatic=False):
    """Create a job unless an identical one is already waiting."""
    existing = DeviceJob.objects.filter(
        job_type=job_type, device=device, employee=employee, status="pending"
    ).first()
    if existing and (payload or {}) == existing.payload:
        return existing
    return DeviceJob.objects.create(
        job_type=job_type, device=device, employee=employee, payload=payload or {},
        created_by=user if (user and user.is_authenticated) else None, is_automatic=automatic,
    )


def _devices(job):
    if job.device_id:
        return [job.device]
    return list(Device.objects.filter(is_active=True))


def _for_each_device(job, report, fn, **kwargs):
    devices = _devices(job)
    if not devices:
        report("No active devices configured.")
    result = {"devices": {}, "failed_devices": []}
    for device in devices:
        try:
            result["devices"][device.name] = fn(device, report, **kwargs)
        except Exception as exc:
            sync.device_failed(device, exc, result, report)
    return result


def _run(job, report):
    t = job.job_type
    p = job.payload or {}
    if t == DeviceJob.TYPE_REFRESH:
        return _for_each_device(job, report, sync.refresh_info)
    if t == DeviceJob.TYPE_PULL_USERS:
        return _for_each_device(job, report, sync.pull_users)
    if t == DeviceJob.TYPE_PUSH_USERS:
        return _for_each_device(job, report, sync.push_users)
    if t == DeviceJob.TYPE_PREVIEW_PUSH:
        return _for_each_device(job, report, sync.push_users, dry_run=True)
    if t == DeviceJob.TYPE_PULL_ATTENDANCE:
        return _for_each_device(job, report, sync.pull_attendance, force=bool(p.get("force")))
    if t == DeviceJob.TYPE_SYNC_TIME:
        return _for_each_device(job, report, sync.sync_time)
    if t == DeviceJob.TYPE_SYNC_USERS:
        return sync.sync_all_devices(report, devices=_devices(job) if job.device_id else None)
    if t == DeviceJob.TYPE_PUSH_EMPLOYEE:
        if not job.employee_id:
            raise ValueError("No employee given")
        return sync.push_employee(job.employee, report)
    if t == DeviceJob.TYPE_REMOVE_USERS:
        return sync.remove_from_all_devices(report)
    if t in (DeviceJob.TYPE_RESTART, DeviceJob.TYPE_CLEAR_ATTENDANCE):
        return _run_maintenance(job, report)
    if t == DeviceJob.TYPE_ENROLL:
        return sync.enroll_fingerprint(job.device, job.employee, int(p.get("finger_index", 6)), report,
                                       replace=bool(p.get("replace")))
    raise ValueError(f"Unknown job type {t}")


def _run_maintenance(job, report):
    """Reboot / clear records on one device, or on the devices the administrator ticked - one after the other,
    so the devices are never all out of service at the same time. Each device gets its own safety checks."""
    p = job.payload or {}
    if job.device_id:
        devices = [job.device]
    else:
        devices = list(Device.objects.filter(pk__in=p.get("device_ids") or []).order_by("name"))
    if not devices:
        raise ValueError("No devices selected")
    clear = job.job_type == DeviceJob.TYPE_CLEAR_ATTENDANCE
    if len(devices) == 1:  # a refusal fails the job, so it is clearly visible
        device = devices[0]
        result = sync.clear_device_attendance(device, report, p.get("period")) if clear \
            else sync.restart_device(device, report)
        return {"devices": {device.name: result}}
    result = {"devices": {}, "failed_devices": []}
    for number, device in enumerate(devices, 1):
        report(f"--- {device.name} ({number} of {len(devices)})")
        try:
            if clear and device.read_only:
                report(f"{device.name}: write-protected (read-only) - skipped")
                result["devices"][device.name] = {"skipped": 1}
            elif clear:
                result["devices"][device.name] = sync.clear_device_attendance(device, report, p.get("period"))
            else:
                result["devices"][device.name] = sync.restart_device(device, report)
        except Exception as exc:  # the other devices still get their turn
            logger.warning("Maintenance on %s failed: %s", device, exc)
            result["failed_devices"].append(device.name)
            report(f"ERROR {device.name}: {exc}")
    return result


def _has_problems(result):
    if not isinstance(result, dict):
        return False
    if result.get("failed_devices"):
        return True
    for section in ("devices", "pull", "push"):
        for stats in (result.get(section) or {}).values():
            if isinstance(stats, dict) and (stats.get("errors") or stats.get("conflicts")):
                return True
    return False


def run_job(job):
    """Run a job that has already been claimed (status=running)."""
    report = sync.Reporter(job)
    if not job.started_at:
        job.started_at = timezone.now()
    status, result = "success", {}
    try:
        report(f"Started: {job.get_job_type_display()} ({job.target_label})")
        result = _run(job, report) or {}
        if _has_problems(result):
            status = "partial"
    except EnrollCancelled as exc:
        report(str(exc))
        status = "cancelled"
        result = {"stage": "cancelled", "message": str(exc)}
    except Exception as exc:
        logger.exception("Job %s failed", job.pk)
        report(f"FAILED: {exc}")
        status = "failed"
        result = {"stage": "failed", "error": str(exc), "message": str(exc)}
    job.refresh_from_db(fields=["log", "progress"])
    job.status = status
    job.result = result
    job.progress = {"success": "Completed", "partial": "Completed with warnings", "cancelled": "Cancelled"}.get(status, "Failed")
    job.finished_at = timezone.now()
    job.save(update_fields=["status", "result", "progress", "started_at", "finished_at"])
    report(f"Finished with status: {job.get_status_display()}")
    return job


def claim_next_job():
    # One device job at a time: devices handle a single session poorly and two writers could race.
    if DeviceJob.objects.filter(status="running").exists():
        return None
    pending = DeviceJob.objects.filter(status="pending")
    # Someone is standing at the device for an enrollment: serve those first.
    candidates = list(pending.filter(job_type=DeviceJob.TYPE_ENROLL).order_by("created_at")[:5]) or \
        list(pending.order_by("created_at")[:5])
    for job in candidates:
        claimed = DeviceJob.objects.filter(pk=job.pk, status="pending").update(
            status="running", started_at=timezone.now()
        )
        if claimed:
            job.refresh_from_db()
            return job
    return None


def run_pending_jobs(limit=None):
    """Run queued jobs synchronously (used by tests and `run_worker --once`)."""
    count = 0
    while limit is None or count < limit:
        job = claim_next_job()
        if job is None:
            break
        run_job(job)
        count += 1
    return count
