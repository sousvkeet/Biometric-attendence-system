import base64
import datetime as dt
import json
import os
import shutil
import tempfile

from django.test import TestCase, override_settings
from zk import ZK

from attendance.models import AttendanceLog
from employees.models import Employee, FingerprintTemplate

from . import sync
from .drivers import DestructiveOperationBlocked, DevFinger, DevUser, SimulatorSession
from .jobs import enqueue, run_pending_jobs
from .models import Device, DeviceJob, DeviceUserLink


def quiet(*args, **kwargs):
    pass


class SimulatorTestCase(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.override = override_settings(SIMULATOR_DIR=self.tmp)
        self.override.enable()

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_device(self, name, **kw):
        return Device.objects.create(name=name, ip_address="sim", driver=Device.DRIVER_SIMULATOR, **kw)

    def seed(self, device, users=(), fingers=(), punches=()):
        s = SimulatorSession(device).open()
        for u in users:
            s.data["users"].append(DevUser(**u).__dict__)
        for uid, fid, tpl in fingers:
            s.data["templates"].append({"uid": uid, "fid": fid, "valid": 1, "template": base64.b64encode(tpl).decode()})
        for user_id, when in punches:
            s.data["attendance"].append({"user_id": user_id, "timestamp": when.isoformat(), "status": 1, "punch": 0})
        s._save()

    def state(self, device):
        with open(os.path.join(self.tmp, f"device_{device.pk}.json")) as fh:
            return json.load(fh)

    def users(self, device):
        return {u["user_id"]: u for u in self.state(device)["users"]}

    def fingers(self, device, uid):
        return sorted(t["fid"] for t in self.state(device)["templates"] if t["uid"] == uid)


class NameMatchTests(TestCase):
    def test_punctuation_and_spacing_ignored(self):
        emp = Employee(device_user_id="501", name="Anil Kumar.Das")
        self.assertTrue(sync.names_compatible(emp, DevUser(uid=1, user_id="501", name="Anil Kumar Das")))
        self.assertTrue(sync.names_compatible(Employee(device_user_id="1", name="Suman  .Moni"),
                                              DevUser(uid=1, user_id="1", name="suman moni")))
        self.assertFalse(sync.names_compatible(Employee(device_user_id="1", name="Ali"),
                                               DevUser(uid=1, user_id="1", name="Alina Z")))
        self.assertFalse(sync.names_compatible(Employee(device_user_id="1", name="Ravi Kumar"),
                                               DevUser(uid=1, user_id="1", name="Zed Other")))


class SafetyTests(TestCase):
    def test_destructive_pyzk_methods_are_blocked(self):
        zk = ZK("127.0.0.1")
        for name in ("delete_user", "delete_user_template", "clear_data", "clear_attendance", "poweroff", "restart"):
            with self.assertRaises(DestructiveOperationBlocked):
                getattr(zk, name)()

    def test_no_delete_calls_in_sync_code(self):
        import inspect

        from . import drivers, jobs
        for module in (sync, jobs):
            src = inspect.getsource(module)
            for name in ("delete_user(", "clear_data(", "clear_attendance(", "delete_user_template("):
                self.assertNotIn(name, src)
        self.assertTrue(hasattr(drivers.ZKSession, "set_user"))
        for name in ("delete_user", "clear_attendance", "clear_data"):
            self.assertFalse(hasattr(drivers.ZKSession, name))
        # The per-user delete commands are reachable from exactly one place: ZKSession.remove_user.
        src = inspect.getsource(drivers)
        remove_src = inspect.getsource(drivers.ZKSession.remove_user)
        self.assertEqual(src.count('_USER_DELETE["'), remove_src.count('_USER_DELETE["'))
        self.assertNotIn("clear_attendance", remove_src)


class MultiDeviceSyncTests(SimulatorTestCase):
    def setUp(self):
        super().setUp()
        from unittest import mock
        patcher = mock.patch.object(SimulatorSession, "ENROLL_STEP_SECONDS", 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.a = self.make_device("A")
        self.b = self.make_device("B")
        self.seed(self.a, users=[
            dict(uid=1, user_id="1", name="Alice", privilege=14),
            dict(uid=2, user_id="2", name="Bob", card=12345, password="11"),
            dict(uid=3, user_id="7", name="Manager Mo", privilege=6),
        ], fingers=[(1, 0, b"alice-0"), (1, 6, b"alice-6"), (2, 6, b"bob-6")])
        # Device B: uid 1 is used by a *different* person (user ID 3) -> must never be overwritten.
        self.seed(self.b, users=[dict(uid=1, user_id="3", name="Carol")], fingers=[(1, 5, b"carol-5")])

    def test_everything_is_copied_everywhere_and_nothing_is_lost(self):
        before_a, before_b = self.state(self.a), self.state(self.b)
        result = sync.sync_all_devices(quiet)
        self.assertEqual(result["failed_devices"], [])
        self.assertEqual(Employee.objects.count(), 4)

        ua, ub = self.users(self.a), self.users(self.b)
        self.assertEqual(set(ua), {"1", "2", "3", "7"})
        self.assertEqual(set(ub), {"1", "2", "3", "7"})
        # Carol keeps uid 1 on B; Alice got a free uid there.
        self.assertEqual(ub["3"]["uid"], 1)
        self.assertEqual(ub["3"]["name"], "Carol")
        self.assertNotEqual(ub["1"]["uid"], 1)
        # Fingerprints propagated in both directions.
        self.assertEqual(self.fingers(self.b, ub["1"]["uid"]), [0, 6])
        self.assertEqual(self.fingers(self.b, ub["2"]["uid"]), [6])
        self.assertEqual(self.fingers(self.a, ua["3"]["uid"]), [5])
        self.assertEqual(self.fingers(self.b, 1), [5])
        # Every original user and template is still present, unchanged.
        for before, device in ((before_a, self.a), (before_b, self.b)):
            after = self.state(device)
            for u in before["users"]:
                self.assertIn(u, after["users"])
            for t in before["templates"]:
                self.assertIn(t, after["templates"])
        # Card/password travel with the user; super admin replicated; manager level not escalated.
        self.assertEqual(ub["2"]["card"], 12345)
        self.assertEqual(ub["2"]["password"], "11")
        self.assertEqual(ub["1"]["privilege"], 14)
        self.assertEqual(ub["7"]["privilege"], 0)
        self.assertEqual(ua["7"]["privilege"], 6)
        self.assertEqual(FingerprintTemplate.objects.count(), 4)

    def test_second_sync_changes_nothing(self):
        sync.sync_all_devices(quiet)
        a1, b1 = self.state(self.a), self.state(self.b)
        result = sync.sync_all_devices(quiet)
        self.assertEqual(self.state(self.a), a1)
        self.assertEqual(self.state(self.b), b1)
        for stats in result["push"].values():
            self.assertEqual(stats["created"] + stats["updated"] + stats["fingerprints_pushed"], 0)

    def test_conflicting_user_id_is_left_untouched(self):
        self.seed(self.b, users=[dict(uid=2, user_id="2", name="Zed Different")])
        sync.sync_all_devices(quiet)
        ub = self.users(self.b)
        self.assertEqual(ub["2"]["name"], "Zed Different")
        self.assertEqual(self.fingers(self.b, 2), [])  # Bob's finger not written onto Zed
        bob = Employee.objects.get(device_user_id="2")
        self.assertFalse(DeviceUserLink.objects.filter(device=self.b, employee=bob).exists())

    def test_read_only_device_is_never_written(self):
        self.b.read_only = True
        self.b.save()
        before = self.state(self.b)
        sync.sync_all_devices(quiet)
        self.assertEqual(self.state(self.b), before)
        # ...but its users are still imported.
        self.assertTrue(Employee.objects.filter(device_user_id="3").exists())

    def test_portal_edit_is_pushed_but_protected_privilege_kept(self):
        sync.sync_all_devices(quiet)
        bob = Employee.objects.get(device_user_id="2")
        bob.name = "Bob Builder"
        bob.save()
        mo = Employee.objects.get(device_user_id="7")
        mo.name = "Mo Renamed"
        mo.save()
        sync.push_users(self.a, quiet)
        ua = self.users(self.a)
        self.assertEqual(ua["2"]["name"], "Bob Builder")
        self.assertEqual(ua["7"]["name"], "Manager Mo")  # privilege 6 user is not rewritten
        self.assertEqual(ua["7"]["privilege"], 6)

    def test_empty_portal_value_does_not_erase_device_value(self):
        sync.sync_all_devices(quiet)
        bob = Employee.objects.get(device_user_id="2")
        bob.card_number = 0
        bob.device_password = ""
        bob.name = "Bob B"
        bob.save()
        sync.push_users(self.a, quiet)
        ua = self.users(self.a)
        self.assertEqual(ua["2"]["card"], 12345)
        self.assertEqual(ua["2"]["password"], "11")
        self.assertEqual(ua["2"]["name"], "Bob B")

    def test_new_portal_employee_registered_on_all_devices(self):
        sync.sync_all_devices(quiet)
        emp = Employee.objects.create(device_user_id="100", name="New Person", card_number=777)
        result = sync.push_employee(emp, quiet)
        self.assertEqual(result["failed_devices"], [])
        for d in (self.a, self.b):
            u = self.users(d)["100"]
            self.assertEqual(u["name"], "New Person")
            self.assertEqual(u["card"], 777)
        self.assertEqual(DeviceUserLink.objects.filter(employee=emp).count(), 2)

    def test_inactive_employee_is_not_pushed_and_not_removed(self):
        sync.sync_all_devices(quiet)
        bob = Employee.objects.get(device_user_id="2")
        bob.status = Employee.STATUS_INACTIVE
        bob.save()
        sync.sync_all_devices(quiet)
        self.assertIn("2", self.users(self.a))
        self.assertIn("2", self.users(self.b))

    def test_device_restricted_employee(self):
        sync.sync_all_devices(quiet)
        emp = Employee.objects.create(device_user_id="200", name="Only A", sync_to_all_devices=False)
        emp.devices.add(self.a)
        sync.push_employee(emp, quiet)
        self.assertIn("200", self.users(self.a))
        self.assertNotIn("200", self.users(self.b))

    def test_enrollment_copies_new_finger_to_other_devices(self):
        sync.sync_all_devices(quiet)
        bob = Employee.objects.get(device_user_id="2")
        sync.enroll_fingerprint(self.a, bob, 3, quiet)
        self.assertTrue(bob.fingerprints.filter(finger_index=3).exists())
        self.assertIn(3, self.fingers(self.b, self.users(self.b)["2"]["uid"]))
        self.assertIn(6, self.fingers(self.a, 2))  # existing finger kept

    def test_preview_writes_nothing_and_predicts_real_upload(self):
        sync.pull_users(self.a, quiet)
        sync.pull_users(self.b, quiet)
        before = self.state(self.b)
        links_before = list(DeviceUserLink.objects.filter(device=self.b).values_list("employee_id", "uid"))
        self.b.read_only = True  # preview is allowed even on write-protected devices
        self.b.save()
        preview = sync.push_users(self.b, quiet, dry_run=True)
        self.assertEqual(self.state(self.b), before)
        self.assertEqual(list(DeviceUserLink.objects.filter(device=self.b).values_list("employee_id", "uid")), links_before)
        self.b.read_only = False
        self.b.save()
        real = sync.push_users(self.b, quiet)
        for key in ("created", "updated", "fingerprints_pushed", "conflicts", "unchanged"):
            self.assertEqual(preview[key], real[key], key)

    def test_offline_device_reported_not_fatal(self):
        self.b.ip_address = "offline"
        self.b.save()
        result = sync.sync_all_devices(quiet)
        self.assertEqual(result["failed_devices"], ["B"])
        self.b.refresh_from_db()
        self.assertEqual(self.b.status, "offline")


class ReviewRegressionTests(SimulatorTestCase):
    def setUp(self):
        super().setUp()
        self.d = self.make_device("A")
        self.seed(self.d, users=[dict(uid=1, user_id="1", name="Alice"), dict(uid=2, user_id="2", name="")],
                  fingers=[(1, 0, b"a0"), (5, 3, b"orphan-of-deleted-user")])
        sync.pull_users(self.d, quiet)

    def test_incomplete_read_aborts_without_writing(self):
        Employee.objects.create(device_user_id="50", name="New")
        before = self.state(self.d)
        from unittest import mock
        with mock.patch.object(SimulatorSession, "get_users", return_value=[]):
            with self.assertRaises(sync.DeviceError):
                sync.push_users(self.d, quiet)
            with self.assertRaises(sync.DeviceError):
                sync.pull_users(self.d, quiet)
        self.assertEqual(self.state(self.d), before)
        self.assertEqual(DeviceUserLink.objects.filter(device=self.d).count(), 2)  # links not wiped

    def test_orphan_template_slot_not_reused(self):
        Employee.objects.create(device_user_id="50", name="New")
        sync.push_users(self.d, quiet)
        new_uid = self.users(self.d)["50"]["uid"]
        self.assertNotIn(new_uid, (1, 2, 5))
        self.assertEqual(self.fingers(self.d, new_uid), [])

    def test_reused_id_on_device_becomes_conflict(self):
        s = SimulatorSession(self.d).open()
        for u in s.data["users"]:
            if u["user_id"] == "1":
                u["name"] = "Zed Other"  # someone re-used ID 1 on the device for another person
        s._save()
        alice = Employee.objects.get(device_user_id="1")
        stats = sync.pull_users(self.d, quiet)
        self.assertEqual(stats["conflicts"], 1)
        self.assertFalse(DeviceUserLink.objects.filter(device=self.d, employee=alice).exists())
        alice.card_number = 999
        alice.save()
        sync.push_users(self.d, quiet)
        self.assertEqual(self.users(self.d)["1"]["name"], "Zed Other")
        self.assertEqual(self.users(self.d)["1"]["card"], 0)

    def test_blank_device_name_not_turned_into_placeholder(self):
        emp = Employee.objects.get(device_user_id="2")
        self.assertEqual(emp.name, "NN-2")
        emp.card_number = 4242
        emp.save()
        sync.push_users(self.d, quiet)
        u = self.users(self.d)["2"]
        self.assertEqual((u["name"], u["card"]), ("", 4242))

    def test_enroll_refused_when_portal_has_finger(self):
        alice = Employee.objects.get(device_user_id="1")
        with self.assertRaises(sync.DeviceError):
            sync.enroll_fingerprint(self.d, alice, 0, quiet)

    def test_periodic_sync_skips_upload_when_up_to_date(self):
        result = sync.sync_all_devices(quiet)
        self.assertEqual(result["push"], {})
        self.assertFalse(sync.needs_push(self.d))
        Employee.objects.create(device_user_id="60", name="Another")
        self.assertTrue(sync.needs_push(self.d))

    def test_user_memory_full_skips_only_new_users(self):
        Employee.objects.create(device_user_id="70", name="Newbie")
        alice = Employee.objects.get(device_user_id="1")
        alice.name = "Alice Renamed"
        alice.save()
        from unittest import mock
        with mock.patch.object(SimulatorSession, "USERS_CAP", 2):
            stats = sync.push_users(self.d, quiet)
        self.assertNotIn("70", self.users(self.d))
        self.assertEqual(self.users(self.d)["1"]["name"], "Alice Renamed")
        self.assertEqual(stats["errors"], 1)


class LiveEnrollmentTests(SimulatorTestCase):
    def setUp(self):
        super().setUp()
        from unittest import mock
        patcher = mock.patch.object(SimulatorSession, "ENROLL_STEP_SECONDS", 0)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.a = self.make_device("A")
        self.b = self.make_device("B")
        self.emp = Employee.objects.create(device_user_id="300", name="Fresh Hire")

    def test_job_records_live_stages_and_copies_to_other_devices(self):
        seen = []
        original = DeviceJob.set_state

        def spy(job, **state):
            seen.append(state.get("stage"))
            original(job, **state)
        from unittest import mock
        with mock.patch.object(DeviceJob, "set_state", spy):
            job = DeviceJob.objects.create(job_type=DeviceJob.TYPE_ENROLL, device=self.a, employee=self.emp,
                                           payload={"finger_index": 6})
            run_pending_jobs()
        job.refresh_from_db()
        self.assertEqual(job.status, "success", job.log)
        for stage in ("preparing", "place", "reading", "accepted", "verifying", "saving", "copying", "done"):
            self.assertIn(stage, seen)
        self.assertEqual(job.result["stage"], "done")
        self.assertEqual(job.result["copied"], ["B"])
        self.assertTrue(self.emp.fingerprints.filter(finger_index=6).exists())
        for d in (self.a, self.b):
            u = self.users(d)["300"]
            self.assertEqual(self.fingers(d, u["uid"]), [6])

    def test_cancel_running_enrollment(self):
        job = DeviceJob.objects.create(job_type=DeviceJob.TYPE_ENROLL, device=self.a, employee=self.emp,
                                       payload={"finger_index": 6}, cancel_requested=True)
        run_pending_jobs()
        job.refresh_from_db()
        self.assertEqual(job.status, "cancelled")
        self.assertFalse(self.emp.fingerprints.exists())

    def test_read_only_device_refused(self):
        self.a.read_only = True
        self.a.save()
        job = DeviceJob.objects.create(job_type=DeviceJob.TYPE_ENROLL, device=self.a, employee=self.emp,
                                       payload={"finger_index": 6})
        run_pending_jobs()
        job.refresh_from_db()
        self.assertEqual(job.status, "failed")
        self.assertIn("write-protected", job.result["message"])

    def test_enrollment_never_touches_another_persons_record(self):
        self.seed(self.a, users=[dict(uid=1, user_id="300", name="Someone Else")])
        with self.assertRaises(sync.DeviceError):
            sync.enroll_fingerprint(self.a, self.emp, 6, quiet)
        self.assertEqual(self.fingers(self.a, 1), [])
        self.assertEqual(self.users(self.a)["300"]["name"], "Someone Else")

    def test_enroll_jobs_jump_the_queue(self):
        enqueue(DeviceJob.TYPE_PULL_ATTENDANCE)
        enr = DeviceJob.objects.create(job_type=DeviceJob.TYPE_ENROLL, device=self.a, employee=self.emp,
                                       payload={"finger_index": 6})
        from .jobs import claim_next_job
        self.assertEqual(claim_next_job().pk, enr.pk)

    def test_enroll_endpoint_json(self):
        from django.contrib.auth import get_user_model
        from django.urls import reverse
        admin = get_user_model().objects.create_superuser("adm", "", "pw123456")
        self.client.force_login(admin)
        url = reverse("employees:enroll", args=[self.emp.pk])
        r = self.client.post(url, {"device": self.a.pk, "finger_index": 3}, HTTP_ACCEPT="application/json")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertTrue(data["ok"])
        status = self.client.get(data["status_url"]).json()
        self.assertEqual(status["state"]["stage"], "queued")
        page = self.client.get(reverse("employees:detail", args=[self.emp.pk]) + "?enroll=1")
        self.assertContains(page, "enrollModal")
        self.assertContains(page, "getOrCreateInstance")


def rt_packet(event, data=b"", tcp=True):
    """Realtime event packet exactly as a ZKTeco device sends it (see zk-protocol realtime docs)."""
    from struct import pack
    body = pack("<4H", 500, 0x12AC, event, 0) + data
    return (pack("<HHI", 0x5050, 0x7D82, len(body)) + body) if tcp else body


class EnrollmentProtocolTests(TestCase):
    def run_stream(self, chunks):
        from .drivers import EventReader, follow_enrollment
        feed = list(chunks)
        acks = []
        reader = EventReader(lambda: feed.pop(0), tcp=True, ack=lambda: acks.append(1))
        stages = []
        ok = follow_enrollment(reader.next_event, lambda **kw: stages.append(kw))
        return ok, stages, acks

    def test_documented_attendance_packet_is_parsed(self):
        from .drivers import EF_ATTLOG, EventReader
        raw = bytes.fromhex("5050827d28000000f401ac1201000000393939313131333333000000000000000000000000000000000001001206191129 05".replace(" ", ""))
        reader = EventReader(lambda: raw, tcp=True, ack=lambda: None)
        event, data = reader.next_event()
        self.assertEqual(event, EF_ATTLOG)
        self.assertEqual(data[:9], b"999111333")

    def test_poor_third_scan_is_retried_not_failed(self):
        """The real-device failure: scans 1-2 good, scan 3 poor (score 0), then a good retry."""
        from .drivers import EF_ENROLLFINGER, EF_FINGER, EF_FPFTR
        from struct import pack
        result_ok = pack("<HH", 0, 1312) + b"901".ljust(9, b"\0") + bytes([6])
        chunks = [rt_packet(EF_FINGER), rt_packet(EF_FPFTR, b"\x64"),
                  rt_packet(EF_FINGER), rt_packet(EF_FPFTR, b"\x64"),
                  rt_packet(EF_FINGER), rt_packet(EF_FPFTR, b"\x00"),
                  rt_packet(EF_FINGER), rt_packet(EF_FPFTR, b"\x64"),
                  rt_packet(EF_ENROLLFINGER, result_ok)]
        ok, stages, acks = self.run_stream(chunks)
        self.assertTrue(ok)
        self.assertIn({"stage": "place", "scan": 3, "total": 3, "retry": True}, stages)
        self.assertEqual([s["scan"] for s in stages if s["stage"] == "accepted"], [1, 2, 3])
        self.assertEqual(len(acks), 9)

    def test_packets_merged_and_split_across_reads(self):
        from .drivers import EF_ENROLLFINGER, EF_FINGER, EF_FPFTR
        stream = b"".join([rt_packet(EF_FINGER), rt_packet(EF_FPFTR, b"\x64")] * 3 +
                          [rt_packet(EF_ENROLLFINGER, b"\x00\x00\x10\x05")])
        chunks = [stream[:5], stream[5:40], stream[40:41], stream[41:]]
        ok, stages, _ = self.run_stream(chunks)
        self.assertTrue(ok)

    def test_unrelated_events_ignored(self):
        from .drivers import EF_ATTLOG, EF_ENROLLFINGER, EF_VERIFY
        ok, _, _ = self.run_stream([rt_packet(EF_ATTLOG, b"x" * 32), rt_packet(EF_VERIFY, b"\xff\xff\xff\xff\x01"),
                                    rt_packet(EF_ENROLLFINGER, b"\x00\x00")])
        self.assertTrue(ok)

    def test_device_errors_are_explained(self):
        from .drivers import EF_ENROLLFINGER
        for code, words in ((5, "already registered"), (6, "did not match"), (4, "stopped on the device"), (9, "code 9")):
            with self.assertRaises(sync.DeviceError) as ctx:
                self.run_stream([rt_packet(EF_ENROLLFINGER, bytes([code, 0]))])
            self.assertIn(words, str(ctx.exception))


class AttendanceSyncTests(SimulatorTestCase):
    def test_pull_is_incremental_and_deduplicated(self):
        d = self.make_device("A")
        t0 = dt.datetime(2026, 9, 1, 9, 0)
        self.seed(d, users=[dict(uid=1, user_id="1", name="Alice")],
                  punches=[("1", t0), ("1", t0 + dt.timedelta(hours=9)), ("99", t0)])
        sync.pull_users(d, quiet)
        r1 = sync.pull_attendance(d, quiet)
        self.assertEqual(r1["new"], 3)
        d.refresh_from_db()
        r2 = sync.pull_attendance(d, quiet)  # count unchanged -> skipped
        self.assertEqual(r2["new"], 0)
        r3 = sync.pull_attendance(d, quiet, force=True)
        self.assertEqual(r3["new"], 0)
        self.assertEqual(AttendanceLog.objects.count(), 3)
        self.assertEqual(AttendanceLog.objects.filter(employee__isnull=True).count(), 1)
        # Device log still has all punches.
        self.assertEqual(len(self.state(d)["attendance"]), 3)
        # Unknown ID gets linked once the employee appears.
        self.seed(d, users=[dict(uid=2, user_id="99", name="Late Joiner")])
        sync.pull_users(d, quiet)
        self.assertEqual(AttendanceLog.objects.filter(employee__isnull=True).count(), 0)

    def test_job_queue_runs_jobs(self):
        d = self.make_device("A")
        self.seed(d, users=[dict(uid=1, user_id="1", name="Alice")])
        job = enqueue(DeviceJob.TYPE_SYNC_USERS)
        self.assertEqual(enqueue(DeviceJob.TYPE_SYNC_USERS).pk, job.pk)  # de-duplicated
        run_pending_jobs()
        job.refresh_from_db()
        self.assertEqual(job.status, "success", job.log)
        self.assertIn("Finished", job.log)
        self.assertTrue(Employee.objects.filter(device_user_id="1").exists())
