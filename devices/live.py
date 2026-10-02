"""
Live punches: one listener thread per device keeps a connection open and receives
attendance events the moment someone punches (ZKTeco realtime EF_ATTLOG events).

* Read-only: the listener only registers for events on its own connection; it never
  writes to the device and never cancels anything running on it (e.g. an enrollment).
* Punches are stored with the same unique key as the periodic download
  (device, user ID, time), so the regular download never duplicates them and also
  fills in anything missed while a listener was reconnecting.
"""
import datetime as dt
import logging
import os
import json
import socket
import threading
import time
from struct import unpack

from django.conf import settings

from backups import maintenance
from django.db import close_old_connections, connection
from django.utils import timezone
from zk import ZK, const

from attendance.models import AttendanceLog
from employees.models import Employee

from .drivers import EF_ATTLOG, EventReader
from .models import Device, DeviceLiveStatus

logger = logging.getLogger("devices")

RECONNECT_EVERY = 600      # refresh the session (and event registration) every 10 minutes
CONFIG_CHECK_EVERY = 15    # seconds between checks whether the device is still enabled
STATUS_REFRESH_EVERY = 30  # heartbeat for the status row


class _Idle(Exception):
    pass


def decode_timehex(raw):
    year, month, day, hour, minute, second = unpack("6B", raw)
    return dt.datetime(2000 + year, month, day, hour, minute, second)


def parse_attlog(data):
    """Decode an EF_ATTLOG payload into [(user_id, naive datetime, verify_type, punch_state)].

    Record layouts used by ZKTeco firmwares (same as pyzk): 12, 32, 36 and 52 bytes.
    """
    records = []
    while len(data) >= 12:
        n = len(data)
        try:
            if n < 32:
                user_id, verify, state, timehex = unpack("<IBB6s", data[:12])
                user_id, size = str(user_id), 12
            else:
                raw_id, verify, state, timehex = unpack("<24sBB6s", data[:32])
                user_id = raw_id.split(b"\x00")[0].decode(errors="ignore")
                size = 52 if n >= 52 else (36 if n >= 36 else 32)
            records.append((user_id, decode_timehex(timehex), verify, state))
        except (ValueError, UnicodeDecodeError):
            logger.warning("Unreadable live attendance record: %s", data[:52].hex())
            break
        data = data[size:]
    return records


def save_live_punch(device, user_id, when, verify, state):
    """Store one punch; returns the AttendanceLog or None when it was already known."""
    punch_time = timezone.make_aware(when, timezone.get_current_timezone())
    emp_id = Employee.objects.filter(device_user_id=user_id).values_list("pk", flat=True).first()
    log, created = AttendanceLog.objects.get_or_create(
        device=device, device_user_id=user_id[:24], punch_time=punch_time,
        defaults=dict(employee_id=emp_id, verify_type=verify, punch_state=state, source=AttendanceLog.SOURCE_DEVICE),
    )
    return log if created else None


def set_status(device_id, **fields):
    DeviceLiveStatus.objects.update_or_create(device_id=device_id, defaults=fields)


class DeviceListener(threading.Thread):
    def __init__(self, device_id, stop_all):
        super().__init__(daemon=True, name=f"live-{device_id}")
        self.device_id = device_id
        self.stop_all = stop_all
        self.stop_me = threading.Event()

    def stopped(self):
        return self.stop_all.is_set() or self.stop_me.is_set()

    def run(self):
        try:
            self._run()
        finally:
            connection.close()  # each thread has its own database connection

    def _run(self):
        backoff = 5
        while not self.stopped():
            close_old_connections()
            device = Device.objects.filter(pk=self.device_id).first()
            if device is None or not (device.is_active and device.live_enabled):
                if device is not None:
                    set_status(self.device_id, status="off", error="")
                return
            try:
                set_status(self.device_id, status="connecting", error="")
                if device.is_simulator:
                    self.listen_simulator(device)
                else:
                    self.listen_zk(device)
                backoff = 5
            except Exception as exc:  # network trouble, device restarted, ...
                logger.warning("Live listener %s: %s", device, exc)
                set_status(self.device_id, status="error", error=str(exc)[:300])
                self.stop_me.wait(backoff)
                backoff = min(backoff * 2, 120)
            finally:
                close_old_connections()

    # ---------------------------------------------------------------- ZKTeco
    def listen_zk(self, device):
        conn = ZK(device.ip_address, port=device.port, timeout=10, password=device.comm_password or 0,
                  ommit_ping=True).connect()
        try:
            conn.reg_event(const.EF_ATTLOG)
            sock = conn._ZK__sock
            sock.settimeout(1.0)

            def receive():
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    raise _Idle
                if not chunk:
                    raise ConnectionError("device closed the live connection")
                return chunk

            reader = EventReader(receive, tcp=conn.tcp, ack=conn._ZK__ack_ok)
            self._mark_connected()
            self._loop(device, lambda: self._next_zk_records(reader))
        finally:
            try:
                conn.disconnect()
            except Exception:
                pass

    def _next_zk_records(self, reader):
        try:
            event, data = reader.next_event()
        except _Idle:
            return []
        if event != EF_ATTLOG:
            return []
        return parse_attlog(data)

    # ---------------------------------------------------------------- simulator
    def listen_simulator(self, device):
        path = os.path.join(settings.SIMULATOR_DIR, f"device_{device.pk}.json")

        def count():
            try:
                with open(path) as fh:
                    return json.load(fh)["attendance"]
            except (OSError, ValueError, KeyError):
                return []

        seen = len(count())
        self._mark_connected()

        def next_records():
            nonlocal seen
            time.sleep(1)
            rows = count()
            new, seen = rows[seen:], len(rows)
            return [(r["user_id"], dt.datetime.fromisoformat(r["timestamp"]), r["status"], r["punch"]) for r in new]

        self._loop(device, next_records)

    # ---------------------------------------------------------------- shared loop
    def _mark_connected(self):
        set_status(self.device_id, status="connected", error="", connected_at=timezone.now())

    def _loop(self, device, next_records):
        started = last_check = last_status = time.monotonic()
        while not self.stopped():
            if maintenance.is_active():
                return  # punches stay on the device and are downloaded after the restore
            for user_id, when, verify, state in next_records():
                try:
                    if save_live_punch(device, user_id, when, verify, state):
                        DeviceLiveStatus.objects.filter(device_id=device.pk).update(last_event_at=timezone.now())
                        logger.info("Live punch %s %s at %s", device.name, user_id, when)
                except Exception:
                    logger.exception("Could not store live punch %s %s", user_id, when)
            now = time.monotonic()
            if now - last_status > STATUS_REFRESH_EVERY:
                DeviceLiveStatus.objects.filter(device_id=device.pk).update(updated_at=timezone.now(), status="connected")
                last_status = now
            if now - last_check > CONFIG_CHECK_EVERY:
                close_old_connections()
                current = Device.objects.filter(pk=device.pk).values("is_active", "live_enabled", "ip_address",
                                                                     "port", "comm_password", "driver").first()
                if not current or not (current["is_active"] and current["live_enabled"]):
                    return
                if (current["ip_address"], current["port"], current["comm_password"], current["driver"]) != (
                        device.ip_address, device.port, device.comm_password, device.driver):
                    return  # settings changed: reconnect with the new ones
                last_check = now
            if now - started > RECONNECT_EVERY:
                return  # periodic fresh session


class LiveManager(threading.Thread):
    """Starts/stops one listener per enabled device (runs inside `manage.py run_worker`)."""

    def __init__(self, stop_all):
        super().__init__(daemon=True, name="live-manager")
        self.stop_all = stop_all
        self.listeners = {}

    def run(self):
        try:
            self._run()
        finally:
            connection.close()

    def _run(self):
        while not self.stop_all.is_set():
            try:
                if maintenance.is_active():  # restore in progress: drop all device connections
                    for listener in self.listeners.values():
                        listener.stop_me.set()
                    self.listeners.clear()
                    self.stop_all.wait(2)
                    continue
                close_old_connections()
                wanted = set(Device.objects.filter(is_active=True, live_enabled=True).values_list("pk", flat=True))
                for pk, listener in list(self.listeners.items()):
                    if pk not in wanted or not listener.is_alive():
                        listener.stop_me.set()
                        del self.listeners[pk]
                for pk in wanted - set(self.listeners):
                    listener = DeviceListener(pk, self.stop_all)
                    listener.start()
                    self.listeners[pk] = listener
                DeviceLiveStatus.objects.exclude(device_id__in=wanted).exclude(status="off").update(status="off")
            except Exception:
                logger.exception("Live manager error")
            self.stop_all.wait(10)
        for listener in self.listeners.values():
            listener.stop_me.set()
