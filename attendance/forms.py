import datetime as dt

from django import forms
from django.utils import timezone

from core.forms import BootstrapMixin
from employees.models import Employee

from .models import Holiday, LeaveRequest, LeaveType, RegularizationRequest, Shift


class ShiftForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Shift
        fields = ["name", "start_time", "end_time", "grace_in_minutes", "grace_out_minutes",
                  "full_day_minutes", "half_day_minutes", "overtime_after_minutes", "weekly_offs",
                  "auto_detect", "detect_before_minutes", "detect_after_minutes"]
        widgets = {"start_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M"),
                   "end_time": forms.TimeInput(attrs={"type": "time"}, format="%H:%M")}


class HolidayForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Holiday
        fields = ["date", "name"]
        widgets = {"date": forms.DateInput(attrs={"type": "date"})}


class LeaveTypeForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = LeaveType
        fields = ["name", "code", "annual_quota", "is_paid"]


class ManualPunchForm(BootstrapMixin, forms.Form):
    employee = forms.ModelChoiceField(queryset=Employee.objects.filter(status="active"))
    punch_time = forms.DateTimeField(widget=forms.DateTimeInput(attrs={"type": "datetime-local"}, format="%Y-%m-%dT%H:%M"))
    punch_state = forms.TypedChoiceField(choices=[(0, "Check-in"), (1, "Check-out")], coerce=int)
    note = forms.CharField(max_length=200, help_text="Reason (kept in the audit log).")

    def clean_punch_time(self):
        value = self.cleaned_data["punch_time"]
        if value > timezone.now() + dt.timedelta(minutes=5):
            raise forms.ValidationError("Punch time cannot be in the future.")
        return value


class LeaveRequestForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = LeaveRequest
        fields = ["employee", "leave_type", "start_date", "end_date", "reason"]
        widgets = {
            "start_date": forms.DateInput(attrs={"type": "date"}),
            "end_date": forms.DateInput(attrs={"type": "date"}),
            "reason": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, employee=None, **kwargs):
        super().__init__(*args, **kwargs)
        if employee is not None:
            self.instance.employee = employee
            del self.fields["employee"]
        else:
            self.fields["employee"].queryset = Employee.objects.filter(status="active")

    def clean(self):
        data = super().clean()
        emp = data.get("employee") or getattr(self.instance, "employee", None)
        start, end = data.get("start_date"), data.get("end_date")
        if emp and start and end:
            if end < start:
                self.add_error("end_date", "End date must be on or after start date.")
            elif LeaveRequest.objects.filter(
                employee=emp, status__in=["pending", "approved"], start_date__lte=end, end_date__gte=start
            ).exclude(pk=self.instance.pk).exists():
                raise forms.ValidationError("There is already a leave request covering these dates.")
        return data


class RegularizationForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = RegularizationRequest
        fields = ["date", "time", "punch_state", "reason"]
        widgets = {
            "date": forms.DateInput(attrs={"type": "date"}),
            "time": forms.TimeInput(attrs={"type": "time"}),
            "reason": forms.Textarea(attrs={"rows": 2}),
        }

    def clean_date(self):
        d = self.cleaned_data["date"]
        if d > timezone.localdate():
            raise forms.ValidationError("Date cannot be in the future.")
        return d


class ReviewForm(forms.Form):
    decision = forms.ChoiceField(choices=[("approve", "Approve"), ("reject", "Reject")])
    note = forms.CharField(max_length=200, required=False)
