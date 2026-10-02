from django.contrib import messages
from django.contrib.auth import get_user_model
from django.db.models import Count, Max, Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from attendance.engine import compute_attendance, summarize
from core.crud import SimpleCrud
from core.decorators import staff_required
from core.pagination import paginate
from core.excel import xlsx_response
from core.models import ActivityLog, SystemSetting
from devices.jobs import enqueue
from devices.models import Device, DeviceJob, DeviceUserLink

from .forms import DepartmentForm, DesignationForm, EmployeeForm, EnrollForm, LoginAccountForm
from .models import Department, Designation, Employee, FingerprintTemplate, RemovedDeviceUser
from .services import DeleteBlocked, delete_employees, forget_removed, restore_employee


def _filtered(request):
    qs = Employee.objects.select_related("department", "designation", "shift").annotate(
        finger_total=Count("fingerprints", distinct=True), device_total=Count("device_links", distinct=True)
    )
    q = request.GET.get("q", "").strip()
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(device_user_id__iexact=q) | Q(email__icontains=q) | Q(phone__icontains=q))
    if request.GET.get("department", "").isdigit():
        qs = qs.filter(department_id=request.GET["department"])
    status = request.GET.get("status", "active")
    if status in ("active", "inactive"):
        qs = qs.filter(status=status)
    fp = request.GET.get("fingerprint")
    if fp == "none":
        qs = qs.filter(finger_total=0)
    elif fp == "yes":
        qs = qs.filter(finger_total__gt=0)
    sort = request.GET.get("sort", "name")
    if sort not in ("name", "device_user_id", "-created_at", "department__name"):
        sort = "name"
    return qs.order_by(sort), q, status


@staff_required
def employee_list(request):
    qs, q, status = _filtered(request)
    if request.GET.get("export") == "xlsx":
        rows = [(e.device_user_id, e.name, e.department.name if e.department else "",
                 e.designation.name if e.designation else "", str(e.shift or ""), e.email, e.phone,
                 e.join_date, e.get_status_display(), e.card_number or "", e.get_privilege_display(),
                 e.finger_total, e.device_total) for e in qs]
        return xlsx_response("employees", "Employees",
                             ["Employee ID", "Name", "Department", "Designation", "Shift", "Email", "Phone",
                              "Join date", "Status", "Card", "Privilege", "Fingerprints", "Devices"], rows,
                             col_widths={2: 28, 3: 18, 4: 18, 5: 22, 6: 24})
    page = paginate(request, qs, 50)
    return render(request, "employees/list.html", {
        "page": page, "q": q, "status": status, "departments": Department.objects.all(),
        "active_devices": Device.objects.filter(is_active=True).count(),
        "removed_count": RemovedDeviceUser.objects.count(),
    })


def _queue_push(request, employee):
    if SystemSetting.get().push_on_employee_save and employee.is_active and employee.target_devices().exists():
        job = enqueue(DeviceJob.TYPE_PUSH_EMPLOYEE, employee=employee, user=request.user)
        messages.info(request, f"Sending {employee.name} to the devices (job #{job.pk}).")


@staff_required
def employee_form(request, pk=None):
    employee = get_object_or_404(Employee, pk=pk) if pk else None
    form = EmployeeForm(request.POST or None, instance=employee)
    if request.method == "POST" and form.is_valid():
        employee = form.save()
        if not pk:
            forget_removed(employee.device_user_id)
        ActivityLog.log(request, "Employee " + ("updated" if pk else "created"), employee,
                        ", ".join(form.changed_data))
        messages.success(request, f"Employee {employee.name} saved.")
        device_fields = {"name", "card_number", "device_password", "privilege", "status", "sync_to_all_devices", "devices"}
        if not pk or device_fields & set(form.changed_data):
            _queue_push(request, employee)
        if not pk and employee.is_active:
            return redirect(employee.get_absolute_url() + "?enroll=1")
        return redirect(employee)
    return render(request, "employees/form.html", {"form": form, "employee": employee})


@staff_required
def employee_detail(request, pk):
    employee = get_object_or_404(Employee.objects.select_related("department", "designation", "shift", "user"), pk=pk)
    today = timezone.localdate()
    month_start = today.replace(day=1)
    days = compute_attendance([employee], month_start, today)[employee.pk]
    links = {l.device_id: l for l in employee.device_links.select_related("device")}
    targets = set(employee.target_devices().values_list("pk", flat=True))
    device_rows = []
    for d in Device.objects.all():
        device_rows.append({"device": d, "link": links.get(d.pk), "target": d.pk in targets})
    return render(request, "employees/detail.html", {
        "employee": employee,
        "fingerprints": employee.fingerprints.select_related("source_device"),
        "device_rows": device_rows,
        "month_days": sorted(days.values(), key=lambda r: r.date, reverse=True),
        "summary": summarize(days.values()),
        "recent_logs": employee.logs.select_related("device")[:15],
        "enroll_form": EnrollForm(),
        "enroll_devices": Device.objects.filter(is_active=True, read_only=False),
        "protected_devices": Device.objects.filter(is_active=True, read_only=True),
        "enrolled_fingers": sorted(employee.fingerprints.values_list("finger_index", flat=True)),
        "open_enroll": request.GET.get("enroll") == "1",
        "jobs": DeviceJob.objects.filter(employee=employee)[:5],
    })


@staff_required
@require_POST
def employee_push(request, pk):
    employee = get_object_or_404(Employee, pk=pk)
    if not employee.is_active:
        messages.warning(request, "Inactive employees are not sent to devices.")
        return redirect(employee)
    job = enqueue(DeviceJob.TYPE_PUSH_EMPLOYEE, employee=employee, user=request.user)
    ActivityLog.log(request, "Employee sent to devices", employee)
    messages.success(request, f"Upload to devices queued (job #{job.pk}).")
    return redirect(job)


@staff_required
@require_POST
def employee_enroll(request, pk):
    employee = get_object_or_404(Employee, pk=pk)
    form = EnrollForm(request.POST)
    if not form.is_valid():
        msg = "Choose a device (not write-protected) and a finger."
        if request.headers.get("Accept") == "application/json":
            return JsonResponse({"ok": False, "error": msg}, status=400)
        messages.error(request, msg)
        return redirect(employee)
    if not employee.is_active:
        if request.headers.get("Accept") == "application/json":
            return JsonResponse({"ok": False, "error": "Employee is inactive."}, status=400)
        messages.error(request, "Inactive employees cannot register fingerprints.")
        return redirect(employee)
    device = form.cleaned_data["device"]
    finger = form.cleaned_data["finger_index"]
    wants_json = request.headers.get("Accept") == "application/json"
    if employee.fingerprints.filter(finger_index=finger).exists():
        msg = "This finger is already registered. Choose another finger."
        if wants_json:
            return JsonResponse({"ok": False, "error": msg}, status=400)
        messages.error(request, msg)
        return redirect(employee)
    job = DeviceJob.objects.create(job_type=DeviceJob.TYPE_ENROLL, device=device, employee=employee,
                                   payload={"finger_index": finger}, created_by=request.user,
                                   result={"stage": "queued", "message": "Waiting for the device..."})
    ActivityLog.log(request, "Fingerprint registration started", employee, f"device {device}, finger {finger}")
    if wants_json:
        return JsonResponse({"ok": True, "job": job.pk, "status_url": reverse("devices:job_status", args=[job.pk]),
                             "cancel_url": reverse("devices:job_cancel", args=[job.pk])})
    messages.info(request, f"Enrollment started on {device.name}. Ask the employee to go to the device.")
    return redirect(job)


@staff_required
def employee_login(request, pk):
    employee = get_object_or_404(Employee, pk=pk)
    User = get_user_model()
    linked = employee.user
    if linked is not None and (linked.is_staff or linked.is_superuser) and not request.user.is_superuser:
        messages.error(request, "This employee is linked to an administrator account. Only the main administrator "
                                "can change it.")
        return redirect(employee)
    if linked is not None and linked.is_superuser and linked != request.user:
        messages.error(request, "Administrator accounts are managed under the user menu → Change password.")
        return redirect(employee)
    initial = {"username": employee.user.username if employee.user_id else employee.device_user_id}
    form = LoginAccountForm(request.POST or None, initial=initial, employee=employee)
    if request.method == "POST":
        if "disable" in request.POST and employee.user_id:
            employee.user.is_active = False
            employee.user.save(update_fields=["is_active"])
            ActivityLog.log(request, "Self-service login disabled", employee)
            messages.success(request, "Login disabled.")
            return redirect(employee)
        if form.is_valid():
            user = employee.user or User(email=employee.email, first_name=employee.name[:150])
            user.username = form.cleaned_data["username"]
            user.is_active = True
            if form.cleaned_data.get("password"):
                user.set_password(form.cleaned_data["password"])
            user.save()
            if not employee.user_id:
                employee.user = user
                employee.save(update_fields=["user"])
            ActivityLog.log(request, "Self-service login saved", employee)
            messages.success(request, f"Login saved. {employee.name} can sign in as '{user.username}'.")
            return redirect(employee)
    return render(request, "employees/login_form.html", {"form": form, "employee": employee})


def _delete_summary(employees):
    ids = [e.pk for e in employees]
    from attendance.models import AttendanceLog, LeaveRequest
    from devices.models import DeviceUserLink
    return {
        "punches": AttendanceLog.objects.filter(employee_id__in=ids).count(),
        "fingerprints": FingerprintTemplate.objects.filter(employee_id__in=ids).count(),
        "device_links": DeviceUserLink.objects.filter(employee_id__in=ids).select_related("device"),
        "leaves": LeaveRequest.objects.filter(employee_id__in=ids).count(),
        "logins": sum(1 for e in employees if e.user_id),
        "device_admins": DeviceUserLink.objects.filter(employee_id__in=ids).exclude(privilege=0).select_related("employee", "device"),
        "writable_devices": Device.objects.filter(is_active=True, read_only=False),
        "protected_devices": Device.objects.filter(is_active=True, read_only=True),
    }


@staff_required
def employee_delete(request, pk):
    employee = get_object_or_404(Employee.objects.select_related("department", "user"), pk=pk)
    if request.method == "POST":
        if request.POST.get("confirm", "").strip() != employee.device_user_id.strip():
            messages.error(request, f"Type the employee ID ({employee.device_user_id.strip()}) to confirm.")
        else:
            from_devices = request.POST.get("scope", "devices") == "devices"
            try:
                _count, job = delete_employees([employee], request, from_devices=from_devices)
            except DeleteBlocked as exc:
                messages.error(request, str(exc))
                return redirect(employee)
            if job:
                messages.success(request, f"{employee.name} was deleted from the portal and is being removed from the devices.")
                return redirect(job)
            messages.success(request, f"{employee.name} was deleted from the portal."
                             + ("" if from_devices else " The devices were not changed."))
            return redirect("employees:list")
    return render(request, "employees/delete_confirm.html", {
        "employees": [employee], "single": employee, "summary": _delete_summary([employee]),
    })


@staff_required
@require_POST
def employee_bulk_delete(request):
    ids = [int(x) for x in request.POST.getlist("ids") if x.isdigit()]
    employees = list(Employee.objects.filter(pk__in=ids).select_related("department").order_by("name"))
    if not employees:
        messages.warning(request, "Select at least one employee.")
        return redirect("employees:list")
    if request.POST.get("confirm") == "DELETE":
        from_devices = request.POST.get("scope", "devices") == "devices"
        try:
            count, job = delete_employees(employees, request, from_devices=from_devices)
        except DeleteBlocked as exc:
            messages.error(request, str(exc))
            return redirect("employees:list")
        if job:
            messages.success(request, f"{count} employee(s) deleted from the portal and being removed from the devices.")
            return redirect(job)
        messages.success(request, f"{count} employee(s) deleted from the portal."
                         + ("" if from_devices else " The devices were not changed."))
        return redirect("employees:list")
    if "confirm" in request.POST:
        messages.error(request, "Type DELETE to confirm.")
    return render(request, "employees/delete_confirm.html", {
        "employees": employees, "single": None, "summary": _delete_summary(employees),
    })


@staff_required
def removed_list(request):
    q = request.GET.get("q", "").strip()
    qs = RemovedDeviceUser.objects.select_related("removed_by").prefetch_related("removals__device")
    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(device_user_id__iexact=q))
    page = paginate(request, qs, 50)
    return render(request, "employees/removed_list.html", {"page": page, "q": q})


@staff_required
@require_POST
def removed_restore(request, pk):
    removed = get_object_or_404(RemovedDeviceUser, pk=pk)
    name, uid = removed.name, removed.device_user_id
    emp, job = restore_employee(removed, request)
    if emp is None and job is None:
        messages.info(request, f"ID {uid} is already used by an employee in the portal.")
    elif emp is None:
        messages.success(request, f"{name} will be re-imported from the devices by job #{job.pk}.")
    else:
        messages.success(request, f"{name} restored with {emp.fingerprints.count()} fingerprint(s) and is being "
                                  f"registered on the devices again (job #{job.pk}).")
        return redirect(emp)
    return redirect("employees:removed")


@staff_required
@require_POST
def deactivate_stale(request):
    """Portal-only cleanup: mark employees without recent punches as inactive (devices are not touched)."""
    try:
        days = max(int(request.POST.get("days", 60)), 7)
    except ValueError:
        days = 60
    cutoff = timezone.now() - timezone.timedelta(days=days)
    stale = Employee.objects.filter(status=Employee.STATUS_ACTIVE).annotate(last=Max("logs__punch_time")).filter(
        Q(last__lt=cutoff) | Q(last__isnull=True, created_at__lt=cutoff)
    )
    ids = list(stale.values_list("pk", flat=True))
    Employee.objects.filter(pk__in=ids).update(status=Employee.STATUS_INACTIVE)
    ActivityLog.log(request, "Bulk deactivate", f"{len(ids)} employees", f"no punch in {days} days")
    messages.success(request, f"{len(ids)} employee(s) with no punch in the last {days} days marked inactive. "
                              "They remain on the devices; you can reactivate anyone from their edit page.")
    return redirect("employees:list")


department_crud = SimpleCrud(
    Department, DepartmentForm, "department", "Departments",
    [("Name", "name"), ("Code", "code"), ("Employees", lambda o: o.employees.filter(status="active").count())],
)
designation_crud = SimpleCrud(
    Designation, DesignationForm, "designation", "Designations",
    [("Name", "name"), ("Employees", lambda o: o.employees.filter(status="active").count())],
)
