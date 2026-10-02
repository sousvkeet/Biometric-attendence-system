"""Employee self-service portal (/me/)."""
import datetime as dt

from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import path
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.decorators import employee_required

from .engine import compute_attendance, summarize
from .forms import LeaveRequestForm, RegularizationForm
from .models import LeaveRequest
from .views import leave_balances, month_end, parse_month


@employee_required
def home(request):
    emp = request.employee
    today = timezone.localdate()
    first = today.replace(day=1)
    days = compute_attendance([emp], first, today)[emp.pk]
    return render(request, "selfservice/home.html", {
        "employee": emp, "today_rec": days[today], "summary": summarize(days.values()),
        "recent_logs": emp.logs.select_related("device")[:10], "balances": leave_balances(emp),
        "pending_leaves": emp.leaves.filter(status="pending"),
        "pending_regs": emp.regularizations.filter(status="pending"),
    })


@employee_required
def my_attendance(request):
    emp = request.employee
    first = parse_month(request.GET.get("month"))
    last = month_end(first)
    days = compute_attendance([emp], first, last)[emp.pk]
    recs = [days[d] for d in sorted(days)]
    return render(request, "selfservice/attendance.html", {
        "employee": emp, "first": first, "recs": recs, "summary": summarize(recs),
        "prev_month": (first - dt.timedelta(days=1)).replace(day=1), "next_month": last + dt.timedelta(days=1),
    })


@employee_required
def my_leaves(request):
    emp = request.employee
    form = LeaveRequestForm(request.POST or None, employee=emp)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Leave request submitted.")
        return redirect("selfservice:leaves")
    return render(request, "selfservice/leaves.html", {
        "form": form, "leaves": emp.leaves.select_related("leave_type"), "balances": leave_balances(emp),
    })


@employee_required
@require_POST
def cancel_leave(request, pk):
    lr = get_object_or_404(LeaveRequest, pk=pk, employee=request.employee, status="pending")
    lr.status = LeaveRequest.STATUS_CANCELLED
    lr.save(update_fields=["status"])
    messages.info(request, "Leave request cancelled.")
    return redirect("selfservice:leaves")


@employee_required
def my_regularizations(request):
    emp = request.employee
    form = RegularizationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        reg = form.save(commit=False)
        reg.employee = emp
        reg.save()
        messages.success(request, "Request submitted for approval.")
        return redirect("selfservice:regularizations")
    return render(request, "selfservice/regularizations.html", {
        "form": form, "items": emp.regularizations.all(),
    })


app_name = "selfservice"
urlpatterns = [
    path("", home, name="home"),
    path("attendance/", my_attendance, name="attendance"),
    path("leaves/", my_leaves, name="leaves"),
    path("leaves/<int:pk>/cancel/", cancel_leave, name="cancel_leave"),
    path("missed-punch/", my_regularizations, name="regularizations"),
]
