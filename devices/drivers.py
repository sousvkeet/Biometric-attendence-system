"""
Device drivers.

Every conversation with a biometric device goes through a *session* object
returned by `open_session(device)`. Sessions expose a deliberately small API:

    read:   info(), get_users(), get_templates(), get_attendance(), get_time()
    write:  set_user(), save_user_template(), set_time(), enroll_finger()
            (add / update only)

There is NO way to delete anything from a device through this module:
users, fingerprints and attendance logs on the hardware are never removed.
As an extra safety net the destructive methods of the pyzk library are
replaced with a function that raises, so even a programming mistake
elsewhere cannot wipe a device.
"""
import base64
import contextlib
import datetime as dt
import functools
import json
import logging
import os
import random
import socket
import threading
import time
from struct import pack, unpack
from dataclasses import asdict, dataclass, field

from django.conf import settings

from zk import ZK, const
from zk.finger import Finger
from zk.user import User

logger = logging.getLogger("devices")

# ---------------------------------------------------------------- safety net
DESTRUCTIVE_PYZK_METHODS = (
    "delete_user",
    "delete_user_template",
    "clear_data",
    "clear_attendance",
    "poweroff",
    "restart",
)


class DestructiveOperationBlocked(RuntimeError):
    pass


def _blocked(name):
    def method(*args, **kwargs):
        raise DestructiveOperationBlocked(
            f"'{name}' is blocked: this system never deletes data from biometric devices."
        )
    method.__name__ = name
    return method


# The originals of the two per-user delete commands are kept privately; the ONLY code allowed to use them is
# ZKSession.remove_user() (removing one specific employee an admin deleted in the portal). Everything else
# stays blocked, and attendance logs can never be deleted.
_USER_DELETE = {name: getattr(ZK, name) for name in ("delete_user", "delete_user_template")}

for _name in DESTRUCTIVE_PYZK_METHODS:
    setattr(ZK, _name, _blocked(_name))

WRITABLE_PRIVILEGES = (0, 14)  # pyzk.set_user silently resets anything else to 0


class DeviceError(Exception):
    pass


class DeviceReadOnly(DeviceError):
    pass


class EnrollCancelled(DeviceError):
    pass


ENROLL_SCANS = 3
ENROLL_TIMEOUT = 60  # seconds the device waits for the finger


# ---------------------------------------------------------------- plain data objects
@dataclass
class DevUser:
    uid: int
    user_id: str
    name: str = ""
    privilege: int = 0
    password: str = ""
    group_id: str = ""
    card: int = 0


@dataclass
class DevFinger:
    uid: int
    fid: int
    valid: int
    template: bytes = field(repr=False)


@dataclass
class DevPunch:
    user_id: str
    timestamp: dt.datetime
    status: int
    punch: int


# ---------------------------------------------------------------- real ZKTeco devices
def _device_call(method):
    """Turn any pyzk/socket error into DeviceError so callers can handle every failure the same way."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except DeviceError:
            raise
        except Exception as exc:
            raise DeviceError(f"{self.device.name}: {method.__name__} failed - {exc}") from exc
    return wrapper


class ZKSession:
    def __init__(self, device, timeout=30):
        self.device = device
        self.read_only = device.read_only
        self._zk = ZK(
            device.ip_address, port=device.port, timeout=timeout,
            password=device.comm_password or 0, ommit_ping=True,
        )
        self.conn = None
        self._disabled = False

    def open(self):
        try:
            self.conn = self._zk.connect()
        except Exception as exc:  # pyzk raises a variety of errors
            raise DeviceError(f"Cannot connect to {self.device.ip_address}:{self.device.port} - {exc}") from exc
        return self

    def close(self):
        if self.conn is None:
            return
        if self._disabled:
            for attempt in range(3):
                try:
                    if attempt:  # the old socket may be broken: re-enable over a fresh connection
                        try:
                            self.conn.disconnect()
                        except Exception:
                            pass
                        self.conn = self._zk.connect()
                    self.conn.enable_device()
                    self._disabled = False
                    break
                except Exception:
                    logger.exception("Could not re-enable device %s (attempt %s)", self.device, attempt + 1)
        try:
            self.conn.disconnect()
        except Exception:
            pass
        self.conn = None

    # ----- reads
    @_device_call
    def info(self):
        c = self.conn
        c.read_sizes()
        data = {
            "serial_number": c.get_serialnumber() or "",
            "platform": c.get_platform() or "",
            "firmware": c.get_firmware_version() or "",
            "device_model": c.get_device_name() or "",
            "fp_version": str(c.get_fp_version() or ""),
            "face_version": str(c.get_face_version() or ""),
            "mac_address": c.get_mac() or "",
            "device_time": c.get_time(),
        }
        data.update(self._sizes())
        return data

    def _sizes(self):
        c = self.conn
        return {
            "users": c.users, "fingers": c.fingers, "records": c.records, "cards": c.cards,
            "faces": getattr(c, "faces", 0) or 0,
            "users_cap": c.users_cap, "fingers_cap": c.fingers_cap, "rec_cap": c.rec_cap,
        }

    @_device_call
    def sizes(self):
        self.conn.read_sizes()
        return self._sizes()

    @property
    def supports_text_user_ids(self):
        return getattr(self.conn, "user_packet_size", 72) != 28

    @_device_call
    def get_users(self):
        return [
            DevUser(uid=u.uid, user_id=str(u.user_id), name=u.name, privilege=u.privilege,
                    password=u.password or "", group_id=u.group_id or "", card=int(u.card or 0))
            for u in self.conn.get_users()
        ]

    @_device_call
    def get_templates(self):
        return [DevFinger(uid=f.uid, fid=f.fid, valid=f.valid, template=f.template) for f in self.conn.get_templates()]

    @_device_call
    def get_attendance(self):
        return [DevPunch(user_id=str(a.user_id), timestamp=a.timestamp, status=a.status, punch=a.punch)
                for a in self.conn.get_attendance()]

    @_device_call
    def get_time(self):
        return self.conn.get_time()

    # ----- writes (add / update only)
    def _check_writable(self):
        if self.read_only:
            raise DeviceReadOnly(f"{self.device.name} is write-protected (read-only).")

    @_device_call
    def lock(self):
        """Temporarily disable the keypad while writing (always re-enabled in close())."""
        self._check_writable()
        if not self._disabled:
            self._disabled = True  # set first: even if the reply is lost, close() will re-enable
            self.conn.disable_device()

    @_device_call
    def set_user(self, user: DevUser):
        self._check_writable()
        if user.privilege not in WRITABLE_PRIVILEGES:
            raise DeviceError(f"Refusing to write privilege {user.privilege} with set_user (would be reset).")
        self.conn.set_user(
            uid=user.uid, name=user.name, privilege=user.privilege, password=user.password or "",
            group_id=user.group_id or "", user_id=str(user.user_id), card=int(user.card or 0),
        )

    @_device_call
    def save_user_template(self, user: DevUser, fingers):
        """Write user record + fingerprints. Callers always pass ALL fingers the user should have
        (existing device fingers + new ones) so nothing already on the device is lost."""
        self._check_writable()
        zk_user = User(user.uid, user.name, user.privilege, user.password or "", user.group_id or "",
                       str(user.user_id), int(user.card or 0))
        zk_fingers = [Finger(user.uid, f.fid, f.valid, bytes(f.template)) for f in fingers]
        self.conn.save_user_template(zk_user, zk_fingers)

    @_device_call
    def set_time(self, when):
        self._check_writable()
        self.conn.set_time(when)

    @_device_call
    def remove_user(self, user: DevUser, finger_ids=()):
        """Delete ONE user (an employee the admin deleted) and any of that user's leftover fingerprints.
        Attendance logs are never touched."""
        self._check_writable()
        _USER_DELETE["delete_user"](self.conn, uid=user.uid)
        for fid in finger_ids:  # templates normally go with the user; clear any that remain
            _USER_DELETE["delete_user_template"](self.conn, uid=user.uid, temp_id=fid)
        self.conn.refresh_data()

    @_device_call
    def enroll_finger(self, user: DevUser, finger_index, progress=None, should_cancel=None, timeout=ENROLL_TIMEOUT,
                      on_event=None):
        """Put the device into fingerprint-enrollment mode for `user` and follow it live.

        Returns True only when the device reports a successful enrollment; raises DeviceError otherwise.
        ``timeout`` is the allowed inactivity (seconds without any device event).
        """
        self._check_writable()
        c = self.conn
        sock = c._ZK__sock
        if c.tcp:
            command_string = pack("<24sbb", str(user.user_id).encode(), finger_index, 1)
        else:
            command_string = pack("<Ib", int(user.user_id), finger_index)

        last_activity = [time.monotonic()]

        def receive():
            while True:
                if should_cancel and should_cancel():
                    raise EnrollCancelled("Enrollment cancelled.")
                if time.monotonic() - last_activity[0] > timeout:
                    raise DeviceError("No finger was placed on the device in time. Please try again.")
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    raise DeviceError("The device closed the connection during enrollment.")
                last_activity[0] = time.monotonic()
                return chunk

        reader = EventReader(receive, tcp=c.tcp, ack=c._ZK__ack_ok)
        c.cancel_capture()
        response = c._ZK__send_command(const.CMD_STARTENROLL, command_string)
        if not response.get("status"):
            raise DeviceError("The device refused to start enrollment (is the user on the device?).")
        try:
            sock.settimeout(1.0)
            return follow_enrollment(reader.next_event, progress, on_event)
        finally:
            sock.settimeout(c._ZK__timeout)
            for cleanup in (c.cancel_capture, c.verify_user):
                try:
                    cleanup()  # always return the device to normal verify mode
                except Exception:
                    logger.warning("Device %s: cleanup after enrollment failed", self.device)


# ---------------------------------------------------------------- realtime enrollment events
# Realtime packets: command CMD_REG_EVENT, the event code travels in the header's session-id field.
EF_ATTLOG, EF_FINGER, EF_ENROLLUSER, EF_ENROLLFINGER, EF_BUTTON, EF_UNLOCK, EF_VERIFY, EF_FPFTR, EF_ALARM = (
    1, 2, 4, 8, 16, 32, 128, 256, 512)
EVENT_NAMES = {EF_ATTLOG: "attendance", EF_FINGER: "finger-touch", EF_ENROLLUSER: "enroll-user",
               EF_ENROLLFINGER: "enroll-result", EF_BUTTON: "button", EF_UNLOCK: "unlock", EF_VERIFY: "verify",
               EF_FPFTR: "scan-quality", EF_ALARM: "alarm"}
ENROLL_ERRORS = {
    4: "The enrollment was stopped on the device (timeout or cancelled at the device). Please try again.",
    5: "This fingerprint is already registered on the device (for this or another person).",
    6: "The scans did not match. Please try again and place the same finger the same way each time.",
}
GOOD_SCAN_SCORE = 100
TCP_MAGIC = (0x5050, 0x7D82)


class EventReader:
    """Splits the byte stream from the device into packets and returns (event, payload) for realtime events."""

    def __init__(self, receive, tcp, ack):
        self.receive = receive
        self.tcp = tcp
        self.ack = ack
        self.buffer = b""

    def _take_packet(self):
        if not self.tcp:
            packet, self.buffer = self.buffer, b""
            return packet or None
        if len(self.buffer) < 8:
            return None
        magic1, magic2, size = unpack("<HHI", self.buffer[:8])
        if (magic1, magic2) != TCP_MAGIC:  # out of sync: drop garbage
            self.buffer = b""
            return None
        if len(self.buffer) < 8 + size:
            return None
        packet, self.buffer = self.buffer[8:8 + size], self.buffer[8 + size:]
        return packet

    def next_event(self):
        while True:
            packet = self._take_packet()
            if packet is None:
                self.buffer += self.receive()
                continue
            if len(packet) < 8:
                continue
            command, _checksum, event, _reply = unpack("<4H", packet[:8])
            self.ack()
            if command != const.CMD_REG_EVENT:
                continue
            return event, packet[8:]


def follow_enrollment(next_event, progress=None, on_event=None, scans=ENROLL_SCANS):
    """Interpret enrollment events. Success/failure is decided only by the device's enroll-result event."""
    progress = progress or (lambda **kw: None)
    good = 0
    progress(stage="place", scan=1, total=scans)
    while True:
        event, data = next_event()
        if on_event:
            on_event(event, data)
        if event == EF_FINGER:
            progress(stage="reading", scan=min(good + 1, scans), total=scans)
        elif event == EF_FPFTR:
            score = data[0] if data else 0
            if score >= GOOD_SCAN_SCORE:
                good += 1
                progress(stage="accepted", scan=min(good, scans), total=scans)
                if good < scans:
                    progress(stage="place", scan=good + 1, total=scans)
                else:
                    progress(stage="verifying", scan=scans, total=scans)
            else:  # poor sample: the device simply asks for the finger again
                progress(stage="place", scan=min(good + 1, scans), total=scans, retry=True)
        elif event == EF_ENROLLFINGER:
            result = unpack("<H", data[:2])[0] if len(data) >= 2 else -1
            if result == 0:
                return True
            raise DeviceError(ENROLL_ERRORS.get(result, f"The device reported enrollment error code {result}."))
        # any other event (attendance, verify, ...) is unrelated and ignored


# ---------------------------------------------------------------- simulator
_sim_lock = threading.Lock()


class SimulatorSession:
    """A virtual device persisted as JSON - lets you try every feature without touching hardware."""

    USERS_CAP, FINGERS_CAP, REC_CAP = 3000, 3000, 100000

    def __init__(self, device, timeout=30):
        self.device = device
        self.read_only = device.read_only
        self.path = os.path.join(settings.SIMULATOR_DIR, f"device_{device.pk}.json")
        self.data = None

    supports_text_user_ids = True

    def open(self):
        if self.device.ip_address.strip().lower() in ("offline", "0.0.0.0"):
            raise DeviceError("Simulator configured as offline")
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with _sim_lock:
            if os.path.exists(self.path):
                with open(self.path) as fh:
                    self.data = json.load(fh)
            else:
                self.data = {"users": [], "templates": [], "attendance": [], "time_offset": 0,
                             "serial": f"SIM{self.device.pk:09d}"}
                self._save()
        return self

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(self.data, fh)
        os.replace(tmp, self.path)

    def close(self):
        self.data = None

    def _now(self):
        return dt.datetime.now().replace(microsecond=0) + dt.timedelta(seconds=self.data.get("time_offset", 0))

    def sizes(self):
        return {
            "users": len(self.data["users"]), "fingers": len(self.data["templates"]),
            "records": len(self.data["attendance"]), "cards": sum(1 for u in self.data["users"] if u.get("card")),
            "faces": 0, "users_cap": self.USERS_CAP, "fingers_cap": self.FINGERS_CAP, "rec_cap": self.REC_CAP,
        }

    def info(self):
        data = {
            "serial_number": self.data["serial"], "platform": "SIMULATOR", "firmware": "Sim 1.0",
            "device_model": "Virtual device", "fp_version": "10", "face_version": "",
            "mac_address": "00:00:00:00:00:00", "device_time": self._now(),
        }
        data.update(self.sizes())
        return data

    def get_users(self):
        return [DevUser(**u) for u in self.data["users"]]

    def get_templates(self):
        return [DevFinger(uid=t["uid"], fid=t["fid"], valid=t["valid"], template=base64.b64decode(t["template"]))
                for t in self.data["templates"]]

    def get_attendance(self):
        return [DevPunch(user_id=a["user_id"], timestamp=dt.datetime.fromisoformat(a["timestamp"]),
                         status=a["status"], punch=a["punch"]) for a in self.data["attendance"]]

    def get_time(self):
        return self._now()

    def _check_writable(self):
        if self.read_only:
            raise DeviceReadOnly(f"{self.device.name} is write-protected (read-only).")

    def lock(self):
        self._check_writable()

    def set_user(self, user: DevUser):
        self._check_writable()
        if user.privilege not in WRITABLE_PRIVILEGES:
            raise DeviceError(f"Refusing to write privilege {user.privilege} with set_user (would be reset).")
        with _sim_lock:
            # Same semantics as the hardware: the record at `uid` is created or overwritten.
            self.data["users"] = [u for u in self.data["users"] if u["uid"] != user.uid]
            self.data["users"].append(asdict(user))
            self.data["users"].sort(key=lambda u: u["uid"])
            self._save()

    def save_user_template(self, user: DevUser, fingers):
        self._check_writable()
        with _sim_lock:
            self.data["users"] = [u for u in self.data["users"] if u["uid"] != user.uid]
            self.data["users"].append(asdict(user))
            self.data["users"].sort(key=lambda u: u["uid"])
            # Fingers sent replace the same finger slots; others are kept.
            sent = {f.fid for f in fingers}
            self.data["templates"] = [t for t in self.data["templates"]
                                      if not (t["uid"] == user.uid and t["fid"] in sent)]
            for f in fingers:
                self.data["templates"].append({"uid": user.uid, "fid": f.fid, "valid": f.valid,
                                               "template": base64.b64encode(bytes(f.template)).decode()})
            self._save()

    def set_time(self, when):
        self._check_writable()
        with _sim_lock:
            self.data["time_offset"] = int((when - dt.datetime.now()).total_seconds())
            self._save()

    def remove_user(self, user: DevUser, finger_ids=()):
        self._check_writable()
        with _sim_lock:
            self.data["users"] = [u for u in self.data["users"] if u["uid"] != user.uid]
            self.data["templates"] = [t for t in self.data["templates"] if t["uid"] != user.uid]
            self._save()

    ENROLL_STEP_SECONDS = 1.5  # simulated time per scan
    SIMULATE_POOR_LAST_SCAN = True

    def enroll_finger(self, user: DevUser, finger_index, progress=None, should_cancel=None, timeout=ENROLL_TIMEOUT,
                      on_event=None):
        self._check_writable()
        progress = progress or (lambda **kw: None)

        def wait(seconds):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                if should_cancel and should_cancel():
                    raise EnrollCancelled("Enrollment cancelled.")
                time.sleep(min(0.2, max(end - time.monotonic(), 0)))

        for scan in range(1, ENROLL_SCANS + 1):
            progress(stage="place", scan=scan, total=ENROLL_SCANS)
            wait(self.ENROLL_STEP_SECONDS)
            progress(stage="reading", scan=scan, total=ENROLL_SCANS)
            wait(self.ENROLL_STEP_SECONDS / 3)
            if scan == ENROLL_SCANS and self.SIMULATE_POOR_LAST_SCAN:  # like a real device: ask again
                progress(stage="place", scan=scan, total=ENROLL_SCANS, retry=True)
                wait(self.ENROLL_STEP_SECONDS)
                progress(stage="reading", scan=scan, total=ENROLL_SCANS)
                wait(self.ENROLL_STEP_SECONDS / 3)
            progress(stage="accepted", scan=scan, total=ENROLL_SCANS)
        progress(stage="verifying", scan=ENROLL_SCANS, total=ENROLL_SCANS)
        fake = bytes(random.getrandbits(8) for _ in range(512))
        self.save_user_template(user, [DevFinger(user.uid, finger_index, 1, fake)])
        return True

    # Simulator-only helper used by the "simulate punch" button and tests.
    def add_punch(self, user_id, when=None, status=1, punch=0):
        with _sim_lock:
            when = when or self._now()
            self.data["attendance"].append({"user_id": str(user_id), "timestamp": when.isoformat(),
                                            "status": status, "punch": punch})
            self._save()


@contextlib.contextmanager
def open_session(device, timeout=None):
    if timeout is None:
        from core.models import SystemSetting
        timeout = SystemSetting.get().device_timeout
    cls = SimulatorSession if device.driver == device.DRIVER_SIMULATOR else ZKSession
    session = cls(device, timeout=timeout).open()
    try:
        yield session
    finally:
        session.close()
