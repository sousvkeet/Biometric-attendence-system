from django import forms
from django.contrib.auth import get_user_model

from core.forms import BootstrapMixin
from devices.models import Device

from .models import FINGER_CHOICES, Department, Designation, Employee


class EmployeeForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Employee
        fields = [
            "device_user_id", "name", "department", "designation", "shift", "email", "phone", "join_date",
            "status", "card_number", "device_password", "privilege", "sync_to_all_devices", "devices", "notes",
        ]
        widgets = {
            "join_date": forms.DateInput(attrs={"type": "date"}),
            "devices": forms.CheckboxSelectMultiple,
            "notes": forms.Textarea(attrs={"rows": 2}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["devices"].queryset = Device.objects.filter(is_active=True)
        from core.models import SystemSetting
        auto = SystemSetting.get().auto_shift_detection
        self.fields["shift"].empty_label = "Auto-detect from punches" if auto else "Default shift"
        self.fields["shift"].help_text = (
            "Choose a fixed shift, or leave on auto-detect: each day's shift is picked from the first punch."
            if auto else "Leave empty to use the default shift.")
        self.fields["card_number"].required = False
        if self.instance.pk:
            self.fields["device_user_id"].disabled = True
            self.fields["device_user_id"].help_text = (
                "The device ID cannot be changed after creation (it identifies the person on every device)."
            )
        else:
            self.fields["device_user_id"].initial = suggest_next_user_id()

    def clean_device_user_id(self):
        value = self.cleaned_data["device_user_id"]
        if self.instance.pk:
            return self.instance.device_user_id  # never changed; may contain legacy characters from a device
        value = value.strip()
        if not value.isascii() or not value.isalnum():
            raise forms.ValidationError("Use letters and digits only (numbers recommended).")
        return value

    def clean_card_number(self):
        return self.cleaned_data.get("card_number") or 0

    def clean_name(self):
        name = " ".join(self.cleaned_data["name"].split())
        if not name:
            raise forms.ValidationError("Name is required.")
        return name


def suggest_next_user_id():
    numbers = [int(x) for x in Employee.objects.values_list("device_user_id", flat=True) if x.isdigit()]
    return str(max(numbers, default=0) + 1)


class DepartmentForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Department
        fields = ["name", "code", "description"]
        widgets = {"description": forms.Textarea(attrs={"rows": 2})}


class DesignationForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Designation
        fields = ["name"]


class EnrollForm(BootstrapMixin, forms.Form):
    device = forms.ModelChoiceField(queryset=Device.objects.none())
    finger_index = forms.TypedChoiceField(choices=FINGER_CHOICES, coerce=int, initial=6, label="Finger")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["device"].queryset = Device.objects.filter(is_active=True, read_only=False)


class LoginAccountForm(BootstrapMixin, forms.Form):
    username = forms.CharField(max_length=150)
    password = forms.CharField(widget=forms.PasswordInput, min_length=6, required=False,
                               help_text="Leave empty to keep the current password.")

    def __init__(self, *args, employee=None, **kwargs):
        self.employee = employee
        super().__init__(*args, **kwargs)

    def clean_username(self):
        username = self.cleaned_data["username"].strip()
        User = get_user_model()
        qs = User.objects.filter(username__iexact=username)
        if self.employee and self.employee.user_id:
            qs = qs.exclude(pk=self.employee.user_id)
        if qs.exists():
            raise forms.ValidationError("This username is already taken.")
        return username

    def clean_password(self):
        password = self.cleaned_data.get("password")
        if password:
            from django.contrib.auth.password_validation import validate_password
            validate_password(password)
        return password

    def clean(self):
        data = super().clean()
        if not (self.employee and self.employee.user_id) and not data.get("password"):
            self.add_error("password", "Password is required for a new account.")
        return data
