import base64
import datetime as dt

from django.db import transaction

from core.models import ActivityLog
from devices.models import Device, DeviceJob, DeviceRemoval

from .models import Employee, FingerprintTemplate, RemovedDeviceUser


class DeleteBlocked(Exception):
    pass


def removed_names():
    """{device_user_id: name} of employees removed from the portal (used to label their punches)."""
    return dict(RemovedDeviceUser.objects.values_list("device_user_id", "name"))


def _backup(e):
    return {
        "name": e.name, "card_number": e.card_number, "device_password": e.device_password, "privilege": e.privilege,
        "email": e.email, "phone": e.phone, "notes": e.notes, "status": e.status,
        "join_date": e.join_date.isoformat() if e.join_date else None,
        "department_id": e.department_id, "designation_id": e.designation_id, "shift_id": e.shift_id,
        "sync_to_all_devices": e.sync_to_all_devices, "device_ids": list(e.devices.values_list("pk", flat=True)),
        "fingerprints": [
            {"finger_index": f.finger_index, "valid": f.valid, "template": base64.b64encode(bytes(f.template)).decode()}
            for f in e.fingerprints.all()
        ],
    }


@transaction.atomic
def delete_employees(employees, request, from_devices=False):
    """Delete employees from the portal, and - when from_devices - queue their removal from every device.

    Kept: their punches (unlinked, still shown under their name) and a backup for Restore.
    Device removal deletes only the person's user record and fingerprints; attendance logs stay on the devices.
    Returns (count, job or None).
    """
    employees = list(employees)
    ids = [e.pk for e in employees]
    if DeviceJob.objects.filter(employee_id__in=ids, status="running").exists():
        raise DeleteBlocked("A device job for this employee is running (e.g. fingerprint registration). "
                            "Wait for it to finish and try again.")
    DeviceJob.objects.filter(employee_id__in=ids, status="pending").update(status="cancelled", progress="Employee deleted")
    devices = list(Device.objects.filter(is_active=True)) if from_devices else []
    names, queued = [], 0
    for e in employees:
        removed, _ = RemovedDeviceUser.objects.update_or_create(
            device_user_id=e.device_user_id,
            defaults=dict(name=e.name, department=e.department.name if e.department_id else "",
                          removed_by=request.user if request.user.is_authenticated else None,
                          remove_from_devices=from_devices, backup=_backup(e)),
        )
        removed.removals.all().delete()
        for d in devices:
            if d.read_only:
                DeviceRemoval.objects.create(removed=removed, device=d, status="skipped", message="Device is write-protected")
            else:
                DeviceRemoval.objects.create(removed=removed, device=d)
                queued += 1
        login = e.user
        e.delete()
        if login is not None and not login.is_staff and not login.is_superuser:
            login.delete()
        names.append(f"{e.name} ({e.device_user_id})")
    job = None
    if queued:
        job = DeviceJob.objects.create(job_type=DeviceJob.TYPE_REMOVE_USERS,
                                       created_by=request.user if request.user.is_authenticated else None)
    ActivityLog.log(request, "Employee deleted" + (" (portal + devices)" if from_devices else " (portal only)"),
                    f"{len(names)} employee(s)", ", ".join(names)[:2000])
    return len(names), job


@transaction.atomic
def restore_employee(removed, request):
    """Re-create a deleted employee from the backup (with fingerprints) and send them to the devices again."""
    b = removed.backup or {}
    if Employee.objects.filter(device_user_id=removed.device_user_id).exists():
        removed.delete()
        return None, None
    if not b:
        removed.delete()  # older record without backup: the next user download re-imports from the devices
        job = DeviceJob.objects.create(job_type=DeviceJob.TYPE_PULL_USERS,
                                       created_by=request.user if request.user.is_authenticated else None)
        return None, job
    emp = Employee.objects.create(
        device_user_id=removed.device_user_id, name=b.get("name") or removed.name,
        card_number=b.get("card_number") or 0, device_password=b.get("device_password") or "",
        privilege=b.get("privilege") or 0, email=b.get("email") or "", phone=b.get("phone") or "",
        notes=b.get("notes") or "", join_date=dt.date.fromisoformat(b["join_date"]) if b.get("join_date") else None,
        department_id=b.get("department_id") if _exists("employees.Department", b.get("department_id")) else None,
        designation_id=b.get("designation_id") if _exists("employees.Designation", b.get("designation_id")) else None,
        shift_id=b.get("shift_id") if _exists("attendance.Shift", b.get("shift_id")) else None,
        sync_to_all_devices=b.get("sync_to_all_devices", True),
    )
    emp.devices.set(Device.objects.filter(pk__in=b.get("device_ids") or []))
    for f in b.get("fingerprints") or []:
        tpl = base64.b64decode(f["template"])
        FingerprintTemplate.objects.create(employee=emp, finger_index=f["finger_index"], valid=f.get("valid", 1),
                                           template=tpl, size=len(tpl))
    from attendance.models import AttendanceLog
    AttendanceLog.objects.filter(employee__isnull=True, device_user_id=emp.device_user_id).update(employee=emp)
    removed.delete()  # also cancels any device removal that has not run yet
    job = DeviceJob.objects.create(job_type=DeviceJob.TYPE_PUSH_EMPLOYEE, employee=emp,
                                   created_by=request.user if request.user.is_authenticated else None)
    ActivityLog.log(request, "Deleted employee restored", emp, f"{len(b.get('fingerprints') or [])} fingerprint(s)")
    return emp, job


def _exists(model_label, pk):
    if not pk:
        return False
    from django.apps import apps
    return apps.get_model(model_label).objects.filter(pk=pk).exists()


def forget_removed(device_user_id):
    """Called when an ID is registered again in the portal."""
    RemovedDeviceUser.objects.filter(device_user_id=device_user_id).delete()
