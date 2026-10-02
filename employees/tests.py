import json
import os
import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from attendance.models import AttendanceLog
from devices import sync
from devices.drivers import SimulatorSession
from devices.jobs import run_pending_jobs
from devices.models import Device, DeviceJob, DeviceUserLink

from .models import Employee, FingerprintTemplate, RemovedDeviceUser


def quiet(*a, **k):
    pass


class DeleteEmployeeTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.override = override_settings(SIMULATOR_DIR=self.tmp)
        self.override.enable()
        self.admin = get_user_model().objects.create_superuser("admin", "", "pw123456")
        self.client.force_login(self.admin)
        self.device = Device.objects.create(name="906", ip_address="sim", driver=Device.DRIVER_SIMULATOR)
        s = SimulatorSession(self.device).open()
        s.data["users"] = [dict(uid=1, user_id="10", name="Ravi Kumar", privilege=0, password="", group_id="", card=0),
                           dict(uid=2, user_id="11", name="Mina Das", privilege=0, password="", group_id="", card=0)]
        import base64
        s.data["templates"] = [{"uid": 1, "fid": 6, "valid": 1, "template": base64.b64encode(b"ravi").decode()}]
        s.data["attendance"] = [{"user_id": "10", "timestamp": "2026-09-30T09:00:00", "status": 1, "punch": 0}]
        s._save()
        sync.pull_users(self.device, quiet)
        self.ravi = Employee.objects.get(device_user_id="10")
        self.ravi.user = get_user_model().objects.create_user("ravi", password="pw123456")
        self.ravi.save()
        AttendanceLog.objects.create(employee=self.ravi, device=self.device, device_user_id="10", punch_time=timezone.now())

    def tearDown(self):
        self.override.disable()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def device_state(self):
        with open(os.path.join(self.tmp, f"device_{self.device.pk}.json")) as fh:
            return json.load(fh)

    def test_wrong_confirmation_keeps_employee(self):
        self.client.post(reverse("employees:delete", args=[self.ravi.pk]), {"confirm": "nope"})
        self.assertTrue(Employee.objects.filter(pk=self.ravi.pk).exists())

    def test_delete_is_portal_only_and_not_reimported(self):
        before = self.device_state()
        self.assertContains(self.client.get(reverse("employees:delete", args=[self.ravi.pk])), "Kept")
        resp = self.client.post(reverse("employees:delete", args=[self.ravi.pk]), {"confirm": "10", "scope": "portal"})
        self.assertRedirects(resp, reverse("employees:list"))
        self.assertFalse(Employee.objects.filter(device_user_id="10").exists())
        self.assertTrue(RemovedDeviceUser.objects.filter(device_user_id="10", name="Ravi Kumar").exists())
        self.assertFalse(FingerprintTemplate.objects.filter(employee_id=self.ravi.pk).exists())
        self.assertFalse(DeviceUserLink.objects.filter(employee_id=self.ravi.pk).exists())
        self.assertFalse(get_user_model().objects.filter(username="ravi").exists())
        log = AttendanceLog.objects.get(device_user_id="10")
        self.assertIsNone(log.employee)
        self.assertEqual(self.device_state(), before)  # device untouched
        # A full sync must neither re-import nor modify the device.
        stats = sync.pull_users(self.device, quiet)
        self.assertEqual(stats["skipped_removed"], 1)
        self.assertFalse(Employee.objects.filter(device_user_id="10").exists())
        sync.sync_all_devices(quiet)
        self.assertEqual(self.device_state(), before)
        # Their punches are labelled by name.
        self.assertContains(self.client.get(reverse("attendance:logs")), "Ravi Kumar")
        feed = self.client.get(reverse("live_feed")).json()
        self.assertIn("Ravi Kumar (removed)", [p["name"] for p in feed["punches"]])

    def test_bulk_delete_needs_confirmation(self):
        ids = list(Employee.objects.values_list("pk", flat=True))
        page = self.client.post(reverse("employees:bulk_delete"), {"ids": ids})
        self.assertContains(page, "Type <code>DELETE</code>")
        self.assertEqual(Employee.objects.count(), 2)
        self.client.post(reverse("employees:bulk_delete"), {"ids": ids, "confirm": "DELETE"})
        self.assertEqual(Employee.objects.count(), 0)
        self.assertEqual(RemovedDeviceUser.objects.count(), 2)

    def test_restore_reimports_with_fingerprint(self):
        self.client.post(reverse("employees:delete", args=[self.ravi.pk]), {"confirm": "10"})
        removed = RemovedDeviceUser.objects.get(device_user_id="10")
        self.client.post(reverse("employees:restore", args=[removed.pk]))
        run_pending_jobs()
        ravi = Employee.objects.get(device_user_id="10")
        self.assertTrue(ravi.fingerprints.filter(finger_index=6).exists())
        self.assertEqual(AttendanceLog.objects.get(device_user_id="10").employee, ravi)

    def test_registering_same_id_again_clears_removed_record(self):
        self.client.post(reverse("employees:delete", args=[self.ravi.pk]), {"confirm": "10"})
        self.client.post(reverse("employees:create"), {"device_user_id": "10", "name": "Ravi Kumar", "status": "active",
                                                       "privilege": 0, "sync_to_all_devices": "on"})
        self.assertTrue(Employee.objects.filter(device_user_id="10").exists())
        self.assertFalse(RemovedDeviceUser.objects.exists())

    def test_running_enrollment_blocks_delete(self):
        DeviceJob.objects.create(job_type=DeviceJob.TYPE_ENROLL, device=self.device, employee=self.ravi, status="running")
        self.client.post(reverse("employees:delete", args=[self.ravi.pk]), {"confirm": "10"})
        self.assertTrue(Employee.objects.filter(pk=self.ravi.pk).exists())

    def test_list_has_bulk_controls(self):
        page = self.client.get(reverse("employees:list"))
        self.assertContains(page, 'id="selectAll"')
        self.assertContains(self.client.get(reverse("employees:removed")), "No removed employees")


class DeleteFromDevicesTests(DeleteEmployeeTests):
    """Deleting with "portal and all devices"."""

    def delete_ravi(self):
        return self.client.post(reverse("employees:delete", args=[self.ravi.pk]), {"confirm": "10", "scope": "devices"})

    def test_removes_only_that_person_and_keeps_device_attendance(self):
        resp = self.delete_ravi()
        job = DeviceJob.objects.get(job_type=DeviceJob.TYPE_REMOVE_USERS)
        self.assertRedirects(resp, job.get_absolute_url(), fetch_redirect_response=False)
        run_pending_jobs()
        job.refresh_from_db()
        self.assertEqual(job.status, "success", job.log)
        state = self.device_state()
        self.assertEqual([u["user_id"] for u in state["users"]], ["11"])       # Mina kept
        self.assertEqual([t for t in state["templates"] if t["uid"] == 1], [])  # Ravi's fingerprints gone
        self.assertEqual(len(state["attendance"]), 1)                           # device attendance untouched
        removal = RemovedDeviceUser.objects.get(device_user_id="10").removals.get()
        self.assertEqual(removal.status, "done")
        self.assertTrue(AttendanceLog.objects.filter(device_user_id="10").exists())  # portal punches kept

    def test_other_person_with_same_id_is_not_deleted(self):
        self.delete_ravi()
        s = SimulatorSession(self.device).open()
        s.data["users"][0]["name"] = "Someone Else"  # ID 10 re-used on the device before the job ran
        s._save()
        run_pending_jobs()
        self.assertIn("10", [u["user_id"] for u in self.device_state()["users"]])
        self.assertEqual(RemovedDeviceUser.objects.get(device_user_id="10").removals.get().status, "skipped")

    def test_write_protected_device_untouched(self):
        self.device.read_only = True
        self.device.save()
        before = self.device_state()
        self.delete_ravi()
        run_pending_jobs()
        self.assertEqual(self.device_state(), before)
        self.assertEqual(RemovedDeviceUser.objects.get(device_user_id="10").removals.get().status, "skipped")

    def test_offline_device_is_retried_later(self):
        self.device.ip_address = "offline"
        self.device.save()
        self.delete_ravi()
        run_pending_jobs()
        removal = RemovedDeviceUser.objects.get(device_user_id="10").removals.get()
        self.assertEqual(removal.status, "failed")
        self.device.ip_address = "sim"
        self.device.save()
        sync.sync_all_devices(quiet)
        removal.refresh_from_db()
        self.assertEqual(removal.status, "done")
        self.assertNotIn("10", [u["user_id"] for u in self.device_state()["users"]])

    def test_restore_puts_person_back_on_devices(self):
        self.delete_ravi()
        run_pending_jobs()
        self.assertNotIn("10", [u["user_id"] for u in self.device_state()["users"]])
        removed = RemovedDeviceUser.objects.get(device_user_id="10")
        self.client.post(reverse("employees:restore", args=[removed.pk]))
        run_pending_jobs()
        ravi = Employee.objects.get(device_user_id="10")
        self.assertEqual(list(ravi.fingerprints.values_list("finger_index", flat=True)), [6])
        user = next(u for u in self.device_state()["users"] if u["user_id"] == "10")
        self.assertEqual(user["name"], "Ravi Kumar")
        self.assertEqual([t["fid"] for t in self.device_state()["templates"] if t["uid"] == user["uid"]], [6])
        self.assertEqual(AttendanceLog.objects.get(device_user_id="10").employee, ravi)

    def test_bulk_delete_from_devices(self):
        ids = list(Employee.objects.values_list("pk", flat=True))
        self.client.post(reverse("employees:bulk_delete"), {"ids": ids, "confirm": "DELETE", "scope": "devices"})
        run_pending_jobs()
        self.assertEqual(self.device_state()["users"], [])
        self.assertEqual(len(self.device_state()["attendance"]), 1)


class DeviceRemovalGuardTests(DeleteEmployeeTests):
    def test_placeholder_names_and_device_admins_not_removed(self):
        s = SimulatorSession(self.device).open()
        s.data["users"].append(dict(uid=3, user_id="12", name="", privilege=0, password="", group_id="", card=0))
        s.data["users"].append(dict(uid=4, user_id="13", name="Dev Admin", privilege=14, password="", group_id="", card=0))
        s._save()
        sync.pull_users(self.device, quiet)
        hr = get_user_model().objects.create_user("hr", password="Hr-pass-1234", is_staff=True)
        self.client.force_login(hr)
        for uid in ("12", "13"):
            emp = Employee.objects.get(device_user_id=uid)
            self.client.post(reverse("employees:delete", args=[emp.pk]), {"confirm": uid, "scope": "devices"})
        run_pending_jobs()
        users = {u["user_id"] for u in self.device_state()["users"]}
        self.assertTrue({"12", "13"} <= users)  # placeholder name + device admin (deleted by non-superuser) kept
        statuses = {r.removed.device_user_id: r.status for r in
                    __import__("devices.models", fromlist=["DeviceRemoval"]).DeviceRemoval.objects.select_related("removed")}
        self.assertEqual(statuses, {"12": "skipped", "13": "skipped"})
