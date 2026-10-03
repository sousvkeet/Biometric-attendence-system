"""
Synchronisation between the portal and the biometric devices.

Rules (designed so that NOTHING on a device is ever deleted or lost):

* Users are identified by their *user ID* (device PIN), which must be the same
  on every device. The internal record number (``uid``) can differ per device.
* Pull (device -> portal): new users found on *enroll* devices become portal
  employees; fingerprint templates are copied into the portal. Empty portal
  fields (card, password) are filled from the device, existing portal values
  are kept.
* Push (portal -> device): missing users are added with a free ``uid`` (never
  re-using a slot that belongs to someone else); existing users are updated
  only if name/card/password differ; fingerprints the device does not have
  yet are added *together with* the fingerprints it already has.
  Privilege levels of users that already exist on a device are never changed.
  Empty portal values never overwrite non-empty device values.
* If a user ID exists on a device with a clearly different name and was never
  linked before, it is reported as a conflict and left untouched.
* Devices marked read-only are never written to.
* Attendance records are only deleted from a device when an administrator asks for it
  (clear_device_attendance), and only after every record is confirmed in the portal.
"""
import calendar
import csv
import datetime as dt
import logging
import re
import time
from collections import Counter, defaultdict

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from attendance.models import AttendanceLog
from employees.models import PRIVILEGE_CHOICES, Employee, FingerprintTemplate, RemovedDeviceUser, device_name_for

from .drivers import (EVENT_NAMES, WRITABLE_PRIVILEGES, DevFinger, DeviceError, DeviceReadOnly, DevUser,
                      EnrollCancelled, open_session)
from .models import Device, DeviceJob, DeviceRemoval, DeviceUserLink

logger = logging.getLogger("devices")

MAX_UID = 65535
VALID_PRIVILEGES = {p for p, _ in PRIVILEGE_CHOICES}


class Reporter:
    """Collects log lines; forwards them to a DeviceJob (if any) so the UI can show progress."""

    def __init__(self, job=None):
        self.job = job

    def __call__(self, message, progress=None):
        logger.info(message)
        if self.job is not None:
            self.job.write(message, progress)

    def state(self, **state):
        if self.job is not None:
            self.job.set_state(**state)

    def cancelled(self):
        return self.job is not None and DeviceJob.objects.filter(pk=self.job.pk, cancel_requested=True).exists()


# ---------------------------------------------------------------- helpers
def _norm(name):
    """Compare names ignoring case, spacing and punctuation ("Anil Kumar.Das" == "Anil Kumar Das")."""
    cleaned = "".join(ch if ch.isalnum() else " " for ch in (name or ""))
    return " ".join(cleaned.split()).casefold()


def is_placeholder_name(name, user_id):
    name = (name or "").strip()
    return not name or name == f"NN-{user_id}"


def names_compatible(employee, dev_user, link=None):
    """True when the device record plausibly belongs to the same person as the portal employee."""
    if is_placeholder_name(dev_user.name, dev_user.user_id) or is_placeholder_name(employee.name, employee.device_user_id):
        return True
    if link is not None and (dev_user.name or "").strip() == (link.device_name or "").strip():
        return True  # device record unchanged since the last sync (e.g. the employee was renamed in the portal)
    a = _norm(device_name_for(employee.name))
    b = _norm(device_name_for(dev_user.name))
    if a == b:
        return True
    short, long_ = sorted((a, b), key=len)
    return long_.startswith(short + " ")  # "Ali" vs "Ali Khan", but not "Ali" vs "Alina"


def read_device_state(session, device, report, attempts=2):
    """Read info, users and templates, and make sure the read is complete.

    A short/failed read must never be mistaken for "the device is empty": that would make occupied
    slots look free. If counts do not match what the device reports, retry, then abort.
    """
    for attempt in range(attempts):
        info = session.info()
        users = session.get_users()
        templates = session.get_templates()
        if len(users) == info["users"] and len(templates) == info["fingers"]:
            return info, users, templates
        report(f"{device.name}: incomplete read (users {len(users)}/{info['users']}, "
               f"fingerprints {len(templates)}/{info['fingers']}) - retrying")
    raise DeviceError(f"{device.name} returned incomplete data; nothing was changed. It will be retried on the next sync.")


def allocate_uid(used_uids):
    candidate = max(used_uids, default=0) + 1
    if candidate <= MAX_UID:
        return candidate
    for uid in range(1, MAX_UID + 1):
        if uid not in used_uids:
            return uid
    raise DeviceError("No free user slot (uid) on the device")


def local_naive_now():
    return timezone.localtime().replace(tzinfo=None, microsecond=0)


def update_device_info(device, info):
    device.serial_number = info.get("serial_number") or device.serial_number
    device.platform = info.get("platform") or device.platform
    device.firmware = info.get("firmware") or device.firmware
    device.device_model = info.get("device_model") or device.device_model
    device.fp_version = info.get("fp_version") or device.fp_version
    device.face_version = info.get("face_version") or ""
    device.mac_address = info.get("mac_address") or device.mac_address
    device.user_count = info.get("users")
    device.fingerprint_count = info.get("fingers")
    device.record_count = info.get("records")
    device.face_count = info.get("faces")
    device.card_count = info.get("cards")
    device.user_capacity = info.get("users_cap") or None
    device.fingerprint_capacity = info.get("fingers_cap") or None
    device.record_capacity = info.get("rec_cap") or None
    device_time = info.get("device_time")
    if device_time:
        device.device_time = timezone.make_aware(device_time)
        device.clock_offset_seconds = int((device_time - local_naive_now()).total_seconds())
    mark_online(device, save=False)
    device.save()


def mark_online(device, save=True):
    device.status = "online"
    device.last_seen = timezone.now()
    device.last_error = ""
    if save:
        device.save(update_fields=["status", "last_seen", "last_error"])


def mark_offline(device, error):
    device.status = "offline"
    device.last_error = str(error)[:1000]
    device.save(update_fields=["status", "last_error"])


def link_orphan_logs():
    """Attach punches that arrived before their employee existed in the portal."""
    orphan_ids = set(
        AttendanceLog.objects.filter(employee__isnull=True).values_list("device_user_id", flat=True).distinct()
    )
    if not orphan_ids:
        return 0
    linked = 0
    for user_id, emp_id in Employee.objects.filter(device_user_id__in=orphan_ids).values_list("device_user_id", "id"):
        linked += AttendanceLog.objects.filter(employee__isnull=True, device_user_id=user_id).update(employee_id=emp_id)
    return linked


# ---------------------------------------------------------------- device info / clock
def refresh_info(device, report):
    with open_session(device) as s:
        info = s.info()
        users = s.get_users()
    device.admin_count = sum(1 for u in users if u.privilege)
    update_device_info(device, info)
    report(
        f"{device.name}: online - {info['users']} users, {info['fingers']} fingerprints, "
        f"{info['records']} attendance records, serial {info.get('serial_number')}"
    )
    return {"users": info["users"], "fingerprints": info["fingers"], "records": info["records"]}


def sync_time(device, report):
    if device.read_only:
        report(f"{device.name}: write-protected - clock not changed")
        return {"skipped": 1}
    with open_session(device) as s:
        before = s.get_time()
        now = local_naive_now()
        s.set_time(now)
        info = s.info()
    update_device_info(device, info)
    drift = int((before - now).total_seconds())
    report(f"{device.name}: clock set to {now:%Y-%m-%d %H:%M:%S} (was off by {drift} s)")
    return {"drift_seconds": drift}


# ---------------------------------------------------------------- pull users (device -> portal)
def pull_users(device, report):
    report(f"{device.name}: reading users and fingerprints...", progress=f"Reading {device.name}")
    with open_session(device) as s:
        info, users, templates = read_device_state(s, device, report)
    device.admin_count = sum(1 for u in users if u.privilege)
    update_device_info(device, info)
    stats = merge_device_users(device, users, templates, report)
    device.last_user_sync = timezone.now()
    device.save(update_fields=["last_user_sync"])
    report(
        f"{device.name}: {len(users)} users read - {stats['created']} new employees, "
        f"{stats['fingerprints_imported']} fingerprints imported, {stats['conflicts']} conflicts"
        + (f", {stats['skipped_removed']} removed employee(s) ignored" if stats["skipped_removed"] else "")
    )
    return dict(stats)


@transaction.atomic
def merge_device_users(device, dev_users, dev_templates, report):
    stats = Counter(created=0, updated=0, linked=0, conflicts=0, fingerprints_imported=0, skipped_unknown=0,
                    skipped_removed=0)
    import_new = device.is_enroll_device
    removed_ids = set(RemovedDeviceUser.objects.values_list("device_user_id", flat=True))
    tpl_by_uid = defaultdict(list)
    for f in dev_templates:
        tpl_by_uid[f.uid].append(f)

    employees = {e.device_user_id: e for e in Employee.objects.all()}
    links = {l.employee_id: l for l in device.user_links.all()}
    portal_fids = defaultdict(set)
    for emp_id, fid in FingerprintTemplate.objects.values_list("employee_id", "finger_index"):
        portal_fids[emp_id].add(fid)

    now = timezone.now()
    seen = set()
    for du in dev_users:
        emp = employees.get(du.user_id)
        if emp is None:
            if du.user_id in removed_ids:  # deleted in the portal; still on the device - do not re-import
                stats["skipped_removed"] += 1
                continue
            if not import_new:
                stats["skipped_unknown"] += 1
                continue
            password = du.password if (du.password or "").isdigit() and len(du.password) <= 8 else ""
            emp = Employee.objects.create(
                device_user_id=du.user_id[:24],
                name=(du.name or f"NN-{du.user_id}")[:120],
                card_number=du.card or 0,
                device_password=password,
                privilege=du.privilege if du.privilege in VALID_PRIVILEGES else 0,
                source_device=device,
            )
            employees[emp.device_user_id] = emp
            stats["created"] += 1
        else:
            if not names_compatible(emp, du, links.get(emp.pk)):
                stats["conflicts"] += 1
                report(
                    f"CONFLICT on {device.name}: user ID {du.user_id} is '{du.name}' on the device but "
                    f"'{emp.name}' in the portal. Left untouched - correct the name or the ID."
                )
                continue
            changed = []
            if not emp.card_number and du.card:
                emp.card_number = du.card
                changed.append("card_number")
            if not emp.device_password and (du.password or "").isdigit() and len(du.password) <= 8:
                emp.device_password = du.password
                changed.append("device_password")
            if changed:
                emp.save(update_fields=changed + ["updated_at"])
                stats["updated"] += 1

        if emp.pk not in links:
            stats["linked"] += 1
        fids = sorted(f.fid for f in tpl_by_uid.get(du.uid, []))
        DeviceUserLink.objects.update_or_create(
            device=device, employee=emp,
            defaults=dict(uid=du.uid, device_name=du.name, privilege=du.privilege, card_number=du.card or 0,
                          fingers=",".join(map(str, fids)), synced_at=now),
        )
        seen.add(emp.pk)

        if import_new:
            for f in tpl_by_uid.get(du.uid, []):
                if f.fid not in portal_fids[emp.pk] and 0 <= f.fid <= 9:
                    FingerprintTemplate.objects.create(
                        employee=emp, finger_index=f.fid, template=bytes(f.template), valid=f.valid,
                        size=len(f.template), source_device=device,
                    )
                    portal_fids[emp.pk].add(f.fid)
                    stats["fingerprints_imported"] += 1

    # Portal bookkeeping only: forget links for users that are no longer on the device.
    device.user_links.exclude(employee_id__in=seen).delete()
    if stats["created"]:
        link_orphan_logs()
    return stats


# ---------------------------------------------------------------- push users (portal -> device)
class PreviewSession:
    """Wraps a session for dry runs: reads go to the device, writes are only reported."""

    def __init__(self, session, report):
        self._s = session
        self._report = report
        self.writes = 0

    def __getattr__(self, name):
        return getattr(self._s, name)

    def lock(self):
        pass

    def set_user(self, user):
        self.writes += 1
        self._report(f"[preview] would write user {user.user_id} '{user.name}' (uid {user.uid}, card {user.card or '-'})")

    def save_user_template(self, user, fingers):
        self.writes += 1
        self._report(f"[preview] would write user {user.user_id} '{user.name}' (uid {user.uid}) with fingers "
                     f"{sorted(f.fid for f in fingers)}")


def employees_for_device(device, employee_ids=None):
    qs = Employee.objects.filter(status=Employee.STATUS_ACTIVE).filter(
        Q(sync_to_all_devices=True) | Q(devices=device)
    ).distinct().order_by("pk")
    if employee_ids is not None:
        qs = qs.filter(pk__in=employee_ids)
    return list(qs)


def push_users(device, report, employee_ids=None, dry_run=False):
    """Upload portal employees to a device. With dry_run=True nothing is written anywhere (device or portal)."""
    stats = Counter(created=0, updated=0, unchanged=0, fingerprints_pushed=0, conflicts=0, errors=0, skipped=0)
    if device.read_only and not dry_run:
        report(f"{device.name}: write-protected (read-only) - nothing uploaded")
        stats["skipped"] += 1
        return dict(stats)
    targets = employees_for_device(device, employee_ids)
    if not targets:
        report(f"{device.name}: no employees to upload")
        return dict(stats)

    portal_tpls = defaultdict(list)
    for t in FingerprintTemplate.objects.filter(employee__in=targets).order_by("finger_index"):
        portal_tpls[t.employee_id].append(t)
    links = {l.employee_id: l for l in device.user_links.all()}
    now = timezone.now()

    report(f"{device.name}: comparing {len(targets)} employee(s) with the device...", progress=f"Uploading to {device.name}")
    with open_session(device) as real_session:
        s = PreviewSession(real_session, report) if dry_run else real_session
        if not dry_run:
            s.lock()  # keypad locked BEFORE reading so nobody can enroll into a slot we think is free
        info, dev_users, dev_tpls = read_device_state(s, device, report)
        by_user_id = {u.user_id: u for u in dev_users}
        # Slots holding leftover fingerprints of deleted users are treated as occupied too.
        used_uids = {u.uid for u in dev_users} | {f.uid for f in dev_tpls}
        tpls_by_uid = defaultdict(dict)
        for f in dev_tpls:
            tpls_by_uid[f.uid][f.fid] = f
        users_free = (info["users_cap"] - info["users"]) if info.get("users_cap") else None
        fp_free = (info["fingers_cap"] - info["fingers"]) if info.get("fingers_cap") else None
        capacity_warned = users_full_warned = False

        def fit_fingers(items):
            nonlocal capacity_warned
            if fp_free is None or len(items) <= fp_free:
                return items
            if not capacity_warned:
                report(f"WARNING {device.name}: fingerprint memory full ({info['fingers_cap']}) - "
                       f"some fingerprints could not be copied")
                capacity_warned = True
            return items[:max(fp_free, 0)]

        # Any exception from here on aborts the whole device session (the connection may be out of sync).
        for emp in targets:
            du = by_user_id.get(emp.device_user_id)
            portal_name = "" if is_placeholder_name(emp.name, emp.device_user_id) else emp.device_name
            if du is not None:
                if not names_compatible(emp, du, links.get(emp.pk)):
                    stats["conflicts"] += 1
                    report(f"CONFLICT on {device.name}: ID {du.user_id} belongs to '{du.name}' on the device, "
                           f"not '{emp.name}'. Not changed.")
                    continue
                current_name = "" if is_placeholder_name(du.name, du.user_id) else du.name
                final = DevUser(
                    uid=du.uid, user_id=du.user_id, name=portal_name or current_name, privilege=du.privilege,
                    password=emp.device_password or du.password or "", group_id=du.group_id,
                    card=emp.card_number or du.card or 0,
                )
                have = tpls_by_uid.get(du.uid, {})
                missing = fit_fingers([t for t in portal_tpls.get(emp.pk, []) if t.finger_index not in have])
                details_changed = (final.name != current_name or final.password != (du.password or "")
                                   or int(final.card) != int(du.card or 0))
                if missing:
                    # Re-send the fingers already on the device together with the new ones.
                    fingers = list(have.values()) + [
                        DevFinger(du.uid, t.finger_index, t.valid, bytes(t.template)) for t in missing
                    ]
                    s.save_user_template(final, fingers)
                    fp_free = None if fp_free is None else fp_free - len(missing)
                    stats["fingerprints_pushed"] += len(missing)
                    stats["updated" if details_changed else "unchanged"] += 1
                    report(f"{device.name}: {emp} - added {len(missing)} fingerprint(s)")
                elif details_changed:
                    if du.privilege not in WRITABLE_PRIVILEGES:
                        stats["skipped"] += 1
                        report(f"{device.name}: {emp} not updated (device privilege {du.privilege} is protected)")
                    else:
                        s.set_user(final)
                        stats["updated"] += 1
                        report(f"{device.name}: updated {emp}")
                else:
                    stats["unchanged"] += 1
                fids = sorted(set(have) | {t.finger_index for t in missing})
            else:
                if not s.supports_text_user_ids and not emp.device_user_id.isdigit():
                    stats["errors"] += 1
                    report(f"{device.name}: {emp} skipped - this device only accepts numeric IDs")
                    continue
                if users_free is not None and users_free <= 0:
                    stats["errors"] += 1
                    if not users_full_warned:
                        report(f"ERROR {device.name}: user memory full ({info['users_cap']}) - new users not added")
                        users_full_warned = True
                    continue
                uid = allocate_uid(used_uids)
                used_uids.add(uid)  # reserve before writing, even if the write later reports an error
                users_free = None if users_free is None else users_free - 1
                final = DevUser(
                    uid=uid, user_id=emp.device_user_id, name=portal_name,
                    privilege=emp.privilege if emp.privilege in WRITABLE_PRIVILEGES else 0,
                    password=emp.device_password or "", group_id="", card=emp.card_number or 0,
                )
                s.set_user(final)
                stats["created"] += 1
                tpls = fit_fingers(portal_tpls.get(emp.pk, []))
                if tpls:
                    s.save_user_template(final, [DevFinger(uid, t.finger_index, t.valid, bytes(t.template)) for t in tpls])
                    fp_free = None if fp_free is None else fp_free - len(tpls)
                    stats["fingerprints_pushed"] += len(tpls)
                fids = sorted(t.finger_index for t in tpls)
                report(f"{device.name}: added {emp} (uid {uid}, {len(tpls)} fingerprint(s))")

            if dry_run:
                continue
            DeviceUserLink.objects.update_or_create(
                device=device, employee=emp,
                defaults=dict(uid=final.uid, device_name=final.name or f"NN-{emp.device_user_id}",
                              privilege=final.privilege, card_number=final.card or 0,
                              fingers=",".join(map(str, fids)), synced_at=now),
            )

        changed = stats["created"] or stats["updated"] or stats["fingerprints_pushed"]
        info = s.sizes() if (changed and not dry_run) else None

    if dry_run:
        report(
            f"{device.name}: PREVIEW ONLY (nothing written) - would add {stats['created']} user(s), update "
            f"{stats['updated']}, copy {stats['fingerprints_pushed']} fingerprint(s); {stats['unchanged']} already "
            f"up to date, {stats['conflicts']} conflict(s), {stats['skipped']} protected"
        )
        return dict(stats)
    if info:
        device.user_count = info["users"]
        device.fingerprint_count = info["fingers"]
        device.save(update_fields=["user_count", "fingerprint_count"])
    device.last_user_sync = timezone.now()
    device.save(update_fields=["last_user_sync"])
    report(
        f"{device.name}: upload done - {stats['created']} added, {stats['updated']} updated, "
        f"{stats['fingerprints_pushed']} fingerprints copied, {stats['conflicts']} conflicts, {stats['errors']} errors"
    )
    return dict(stats)


# ---------------------------------------------------------------- attendance (device -> portal)
def pull_attendance(device, report, force=False):
    report(f"{device.name}: checking attendance records...", progress=f"Attendance from {device.name}")
    with open_session(device) as s:
        info = s.info()
        update_device_info(device, info)
        if not force and device.last_record_count is not None and info["records"] == device.last_record_count:
            device.last_attendance_sync = timezone.now()
            device.save(update_fields=["last_attendance_sync"])
            report(f"{device.name}: no new records ({info['records']} on device)")
            return {"new": 0, "on_device": info["records"]}
        punches = s.get_attendance()
    new = save_punches(device, punches)
    device.last_record_count = info["records"]
    device.last_attendance_sync = timezone.now()
    device.save(update_fields=["last_record_count", "last_attendance_sync"])
    report(f"{device.name}: {len(punches)} records read, {new} new punches saved")
    return {"new": new, "on_device": len(punches)}


def save_punches(device, punches):
    existing = set(AttendanceLog.objects.filter(device=device).values_list("device_user_id", "punch_time"))
    emp_map = dict(Employee.objects.values_list("device_user_id", "id"))
    tz = timezone.get_current_timezone()
    new_rows = []
    for p in punches:
        ts = timezone.make_aware(p.timestamp, tz)
        key = (p.user_id, ts)
        if key in existing:
            continue
        existing.add(key)
        new_rows.append(AttendanceLog(
            employee_id=emp_map.get(p.user_id), device=device, device_user_id=p.user_id[:24], punch_time=ts,
            verify_type=p.status, punch_state=p.punch, source=AttendanceLog.SOURCE_DEVICE,
        ))
    AttendanceLog.objects.bulk_create(new_rows, batch_size=2000, ignore_conflicts=True)
    return len(new_rows)


# ---------------------------------------------------------------- maintenance (manual, admin only)
REBOOT_FIRST_WAIT = 15  # seconds before trying to reconnect after a reboot command
REBOOT_RETRY_EVERY = 5
REBOOT_GIVE_UP = 180


def restart_device(device, report):
    """Reboot a device and wait until it answers again. Nothing on the device is deleted."""
    report(f"{device.name}: sending the reboot command...", progress=f"Rebooting {device.name}")
    with open_session(device) as s:
        s.restart()
    report(f"{device.name}: rebooting - waiting for it to come back (up to {REBOOT_GIVE_UP // 60} minutes)...",
           progress=f"Waiting for {device.name}")
    started = time.monotonic()
    time.sleep(REBOOT_FIRST_WAIT)
    while True:
        try:
            with open_session(device, timeout=10) as s:
                info = s.info()
            update_device_info(device, info)
            seconds = int(time.monotonic() - started)
            report(f"{device.name}: back online after {seconds} s - {info['users']} users, "
                   f"{info['records']} attendance records")
            return {"rebooted": 1, "back_after_seconds": seconds}
        except DeviceError as exc:
            if time.monotonic() - started >= REBOOT_GIVE_UP:
                mark_offline(device, f"Not reachable after reboot: {exc}")
                report(f"WARNING {device.name}: not reachable {REBOOT_GIVE_UP // 60} minutes after the reboot. "
                       "Check that it started normally and is connected to the network.")
                return {"rebooted": 1, "errors": 1}
            time.sleep(REBOOT_RETRY_EVERY)


def _punch_key(device_user_id, aware_time):
    return device_user_id[:24], aware_time


def _write_safety_copy(device, punches):
    """CSV copy of the raw device records, kept in the data folder before anything is deleted."""
    folder = settings.VAR_DIR / "device-records"
    folder.mkdir(parents=True, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9_-]+", "-", device.name).strip("-") or f"device-{device.pk}"
    path = folder / f"{safe_name}-{timezone.localtime():%Y%m%d-%H%M%S}.csv"
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["device", "serial_number", "user_id", "punch_time", "verify_type", "punch_state"])
        for p in punches:
            writer.writerow([device.name, device.serial_number, p.user_id, p.timestamp.isoformat(sep=" "),
                             p.status, p.punch])
    return path


# Periods offered on the clear form. The device itself can only be cleared completely (ZKTeco has no command
# to delete by date); the chosen period is confirmed in the portal and linked from the job report.
KEEP_PERIODS = [  # (code, label, months)
    ("6m", "Last 6 months", 6), ("1y", "Last 1 year", 12), ("18m", "Last 1.5 years", 18),
    ("2y", "Last 2 years", 24), ("30m", "Last 2.5 years", 30), ("3y", "Last 3 years", 36),
]


def months_before(day, months):
    year, month = divmod(day.year * 12 + day.month - 1 - months, 12)
    month += 1
    return day.replace(year=year, month=month, day=min(day.day, calendar.monthrange(year, month)[1]))


def keep_period(code, date_from=None, date_to=None, today=None):
    """The period chosen on the clear form -> {"label", "from", "to"} (ISO dates). Raises ValueError."""
    today = today or timezone.localdate()
    for period_code, label, months in KEEP_PERIODS:
        if code == period_code:
            return {"label": label, "from": months_before(today, months).isoformat(), "to": today.isoformat()}
    if code == "range":
        if not (date_from and date_to):
            raise ValueError("Choose the start and end date of the records to keep.")
        if date_from > date_to:
            raise ValueError("The start date must be before the end date.")
        return {"label": f"{date_from:%d %b %Y} to {date_to:%d %b %Y}",
                "from": date_from.isoformat(), "to": date_to.isoformat()}
    raise ValueError("Choose which records to keep.")


def portal_punches_between(device, start, end):
    """Portal punches from `device` (None: from any device) between two dates."""
    tz = timezone.get_current_timezone()
    return AttendanceLog.objects.filter(
        **({"device": device} if device is not None else {"device__isnull": False}),
        punch_time__gte=timezone.make_aware(dt.datetime.combine(start, dt.time.min), tz),
        punch_time__lt=timezone.make_aware(dt.datetime.combine(end + dt.timedelta(days=1), dt.time.min), tz),
    )


def clear_device_attendance(device, report, period=None):
    """Free the attendance memory of a device (manual, administrator only).

    Order of safety checks - if any fails, nothing is deleted:
      1. the keypad is locked, so no punch can arrive between checking and clearing;
      2. all records are read (the count must match what the device reports);
      3. they are saved in the portal, and EVERY record is then looked up in the portal database;
      4. a CSV safety copy is written to the data folder;
      5. the device must still report the same number of records.
    `period` (from keep_period) names the records the administrator wants to keep: they are confirmed in the
    portal separately and reported. Only the attendance records are cleared: users, fingerprints and cards stay.
    """
    if device.read_only:
        raise DeviceReadOnly(f"{device.name} is write-protected (read-only): attendance records not cleared.")
    report(f"{device.name}: locking the keypad and reading all attendance records...",
           progress=f"Reading {device.name}")
    with open_session(device) as s:
        s.lock()
        info = s.info()
        update_device_info(device, info)
        punches = s.get_attendance()
        if len(punches) != info["records"]:
            raise DeviceError(f"{device.name}: read {len(punches)} of {info['records']} records - incomplete read, "
                              "nothing was deleted. Try again.")
        if not punches:
            report(f"{device.name}: the device has no attendance records - nothing to clear")
            device.last_record_count = 0
            device.save(update_fields=["last_record_count"])
            return {"cleared": 0, "new": 0}

        new = save_punches(device, punches)
        tz = timezone.get_current_timezone()
        in_portal = {_punch_key(uid, t) for uid, t in
                     AttendanceLog.objects.filter(device=device).values_list("device_user_id", "punch_time")}
        missing = [p for p in punches
                   if _punch_key(p.user_id, timezone.make_aware(p.timestamp, tz)) not in in_portal]
        if missing:
            raise DeviceError(f"{device.name}: {len(missing)} record(s) could not be confirmed in the portal - "
                              "nothing was deleted.")
        report(f"{device.name}: all {len(punches)} records are saved in the portal ({new} were new)")
        kept = None
        if period:
            start, end = dt.date.fromisoformat(period["from"]), dt.date.fromisoformat(period["to"])
            on_device = sum(1 for p in punches if start <= p.timestamp.date() <= end)
            in_portal_count = portal_punches_between(device, start, end).count()
            if in_portal_count < on_device:  # cannot happen after the check above; never clear if it does
                raise DeviceError(f"{device.name}: the portal has fewer records for {period['label']} than the "
                                  "device - nothing was deleted.")
            kept = {**period, "device_records": on_device, "portal_punches": in_portal_count}
            report(f"{device.name}: records to keep - {period['label']} ({start:%d %b %Y} to {end:%d %b %Y}): "
                   f"all {on_device} device records are in the portal ({in_portal_count} punches from this "
                   "device in that period). They stay in the portal.")
        copy = _write_safety_copy(device, punches)
        report(f"{device.name}: safety copy written to {copy}")

        if s.sizes()["records"] != len(punches):
            raise DeviceError(f"{device.name}: the number of records changed while checking - nothing was deleted. "
                              "Try again.")
        report(f"{device.name}: clearing attendance records on the device...", progress=f"Clearing {device.name}")
        s.clear_attendance()
        after = s.sizes()

    device.record_count = after["records"]
    device.last_record_count = after["records"]  # so the next download does not skip new punches
    device.last_attendance_sync = timezone.now()
    device.save(update_fields=["record_count", "last_record_count", "last_attendance_sync"])
    cleared = len(punches) - after["records"]
    report(f"{device.name}: {cleared} attendance records cleared from the device "
           f"({after['records']} left, capacity {after.get('rec_cap') or '?'}). "
           "Users and fingerprints were not touched.")
    result = {"cleared": cleared, "new": new, "safety_copy": str(copy)}
    if kept:
        result["kept"] = kept
    if after["records"]:
        result["errors"] = 1
        report(f"WARNING {device.name}: the device still reports {after['records']} records after clearing")
    return result


# ---------------------------------------------------------------- remove deleted employees from a device
def process_removals(device, report):
    """Delete from this device the employees an admin deleted in the portal with "also from devices".

    Only the matching user record (same ID and same person) and its fingerprints are deleted.
    Attendance logs on the device are never touched.
    """
    pending = list(DeviceRemoval.objects.filter(device=device, status__in=["pending", "failed"])
                   .select_related("removed", "removed__removed_by"))
    stats = Counter(removed=0, not_on_device=0, skipped=0)
    if not pending:
        return dict(stats)
    if device.read_only:
        for r in pending:
            r.mark("skipped", "Device is write-protected")
        report(f"{device.name}: write-protected - {len(pending)} deleted employee(s) left on the device")
        stats["skipped"] += len(pending)
        return dict(stats)
    report(f"{device.name}: removing {len(pending)} deleted employee(s) from the device...",
           progress=f"Removing from {device.name}")
    try:
        with open_session(device) as s:
            s.lock()
            info, users, templates = read_device_state(s, device, report)
            by_id = {u.user_id: u for u in users}
            fingers = defaultdict(list)
            for f in templates:
                fingers[f.uid].append(f.fid)
            for r in pending:
                du = by_id.get(r.removed.device_user_id)
                if du is None:
                    r.mark("done", "Was not on this device")
                    stats["not_on_device"] += 1
                    continue
                probe = Employee(device_user_id=r.removed.device_user_id, name=r.removed.name)
                if is_placeholder_name(du.name, du.user_id) or is_placeholder_name(r.removed.name, r.removed.device_user_id):
                    r.mark("skipped", "Name unknown on the device or in the portal - not deleted (remove it on the device)")
                    report(f"{device.name}: NOT deleted ID {du.user_id}: the name cannot be confirmed")
                    stats["skipped"] += 1
                    continue
                if du.privilege and not (r.removed.removed_by and r.removed.removed_by.is_superuser):
                    r.mark("skipped", "Device administrator - only the main administrator can remove it")
                    report(f"{device.name}: NOT deleted ID {du.user_id}: device administrator")
                    stats["skipped"] += 1
                    continue
                if not names_compatible(probe, du):
                    r.mark("skipped", f"ID belongs to '{du.name}' on the device - not deleted")
                    report(f"{device.name}: NOT deleted ID {du.user_id}: it belongs to '{du.name}', not '{r.removed.name}'")
                    stats["skipped"] += 1
                    continue
                s.remove_user(du, finger_ids=fingers.get(du.uid, []))
                r.mark("done", f"Removed (uid {du.uid}, {len(fingers.get(du.uid, []))} fingerprint(s))")
                report(f"{device.name}: removed {r.removed.name} ({du.user_id}) and {len(fingers.get(du.uid, []))} fingerprint(s)")
                stats["removed"] += 1
            sizes = s.sizes()
    except DeviceError as exc:
        for r in pending:
            r.refresh_from_db()
            if r.status == "pending":
                r.mark("failed", f"Device not reachable - will retry ({str(exc)[:120]})")
        raise
    device.user_count, device.fingerprint_count = sizes["users"], sizes["fingers"]
    device.save(update_fields=["user_count", "fingerprint_count"])
    return dict(stats)


def remove_from_all_devices(report):
    device_ids = DeviceRemoval.objects.filter(status__in=["pending", "failed"]).values_list("device_id", flat=True)
    result = {"devices": {}, "failed_devices": []}
    for d in Device.objects.filter(pk__in=set(device_ids), is_active=True):
        try:
            result["devices"][d.name] = process_removals(d, report)
        except Exception as exc:
            device_failed(d, exc, result, report)
    return result


# ---------------------------------------------------------------- multi-device sync
def needs_push(device):
    """Cheap check (portal data only) whether an upload could change anything on the device.
    Lets the periodic sync avoid locking a device's keypad when there is nothing to do."""
    links = {l.employee_id: l for l in device.user_links.all()}
    portal_fids = defaultdict(set)
    for emp_id, fid in FingerprintTemplate.objects.values_list("employee_id", "finger_index"):
        portal_fids[emp_id].add(fid)
    free = device.fingerprint_free
    for emp in employees_for_device(device):
        link = links.get(emp.pk)
        if link is None:
            return True
        if (portal_fids[emp.pk] - set(link.finger_list)) and (free is None or free > 0):
            return True
        portal_name = "" if is_placeholder_name(emp.name, emp.device_user_id) else emp.device_name
        dev_name = "" if is_placeholder_name(link.device_name, emp.device_user_id) else link.device_name
        if portal_name and portal_name != dev_name and link.privilege in WRITABLE_PRIVILEGES:
            return True
        if emp.card_number and emp.card_number != link.card_number and link.privilege in WRITABLE_PRIVILEGES:
            return True
    return False


def sync_all_devices(report, devices=None):
    """Collect users/fingerprints from all devices, then make every device contain every employee."""
    devices = list(devices if devices is not None else Device.objects.filter(is_active=True))
    result = {"pull": {}, "push": {}, "failed_devices": []}
    reachable = []
    for d in devices:
        try:
            if DeviceRemoval.objects.filter(device=d, status__in=["pending", "failed"]).exists():
                process_removals(d, report)  # finish deletions that could not run earlier (device was offline)
            result["pull"][d.name] = pull_users(d, report)
            reachable.append(d)
        except Exception as exc:
            device_failed(d, exc, result, report)
    for d in reachable:
        if d.read_only:
            report(f"{d.name}: write-protected (read-only) - nothing uploaded")
            continue
        if not needs_push(d):
            report(f"{d.name}: already up to date - nothing to upload")
            continue
        try:
            result["push"][d.name] = push_users(d, report)
        except Exception as exc:
            device_failed(d, exc, result, report)
    return result


def device_failed(device, exc, result, report):
    if isinstance(exc, DeviceError):
        mark_offline(device, exc)
    else:
        logger.exception("Unexpected error with %s", device)
    result["failed_devices"].append(device.name)
    report(f"ERROR {device.name}: {exc}")


def push_employee(employee, report):
    result = {"devices": {}, "failed_devices": []}
    if not employee.is_active:
        report(f"{employee} is inactive - not sent to devices (nothing is deleted from devices).")
        return result
    devices = list(employee.target_devices())
    if not devices:
        report("No active devices for this employee.")
    for d in devices:
        try:
            result["devices"][d.name] = push_users(d, report, employee_ids=[employee.pk])
        except Exception as exc:
            device_failed(d, exc, result, report)
    return result


def enroll_fingerprint(device, employee, finger_index, report):
    """Start fingerprint enrollment on the device and follow it live; then save the template in the
    portal and copy it to the employee's other devices."""
    state = getattr(report, "state", lambda **kw: None)
    should_cancel = getattr(report, "cancelled", lambda: False)
    base = {"device": device.name, "finger": finger_index}

    def stage(name, message, **extra):
        state(stage=name, message=message, **base, **extra)

    if device.read_only:
        raise DeviceError(f"{device.name} is write-protected; turn off write protection to register fingerprints.")
    if employee.fingerprints.filter(finger_index=finger_index).exists():
        raise DeviceError("This finger is already registered for the employee. Choose another finger.")

    stage("preparing", f"Registering {employee.name} on {device.name}...")
    report(f"Making sure {employee} exists on {device.name}...", progress="Preparing device")
    push_users(device, report, employee_ids=[employee.pk])
    if should_cancel():
        raise EnrollCancelled("Enrollment cancelled.")
    link = DeviceUserLink.objects.filter(device=device, employee=employee).first()
    if link is None:
        raise DeviceError(f"{employee.name} could not be registered on {device.name} (see the log: ID conflict, "
                          f"memory full or employee not assigned to this device).")

    with open_session(device, timeout=30) as s:
        du = next((u for u in s.get_users() if u.user_id == employee.device_user_id), None)
        if du is None or du.uid != link.uid or not names_compatible(employee, du, link):
            raise DeviceError(f"The record for ID {employee.device_user_id} on {device.name} does not match "
                              f"{employee.name}; enrollment stopped to protect the other record.")
        if finger_index in {f.fid for f in s.get_templates() if f.uid == du.uid}:
            raise DeviceError("That finger is already registered on this device. Choose another finger "
                              "(existing fingerprints are never overwritten).")

        messages = {
            "place": "Place the finger on the device sensor (scan {scan} of {total})",
            "reading": "Reading finger... (scan {scan} of {total})",
            "accepted": "Scan {scan} of {total} accepted - lift the finger",
            "verifying": "Checking the fingerprint...",
        }

        def on_progress(stage, scan, total, retry=False):
            if retry:
                text = f"Scan not clear - place the same finger again (scan {scan} of {total})"
            else:
                text = messages[stage].format(scan=scan, total=total)
            state(stage=stage, scan=scan, total=total, retry=retry, message=text, **base)
            report(text, progress=text)

        def on_event(event, data):
            name = EVENT_NAMES.get(event, f"event-{event}")
            report(f"  [device] {name} data={data.hex() or '-'}")

        report(f"{device.name} is waiting for {employee.name}'s finger.")
        s.enroll_finger(du, finger_index, progress=on_progress, should_cancel=should_cancel, on_event=on_event)
        new = [f for f in s.get_templates() if f.uid == du.uid and f.fid == finger_index]
    if not new:
        raise DeviceError("The device reported success but no fingerprint was stored. Please try again.")
    f = new[0]
    stage("saving", "Fingerprint captured - saving...")
    FingerprintTemplate.objects.get_or_create(
        employee=employee, finger_index=finger_index,
        defaults=dict(template=bytes(f.template), valid=f.valid, size=len(f.template), source_device=device),
    )
    link.fingers = ",".join(map(str, sorted({*link.finger_list, finger_index})))
    link.save(update_fields=["fingers"])
    report("Fingerprint captured and saved in the portal.")

    copied, failed = [], []
    others = [d for d in employee.target_devices() if d.pk != device.pk and not d.read_only]
    for d in others:
        stage("copying", f"Copying fingerprint to {d.name}...", copied=copied)
        try:
            push_users(d, report, employee_ids=[employee.pk])
            copied.append(d.name)
        except Exception as exc:
            failed.append(d.name)
            device_failed(d, exc, {"failed_devices": []}, report)
    done = {"stage": "done", "message": "Fingerprint registered", "copied": copied, "copy_failed": failed, **base}
    state(**done)
    return done
