from django import forms

from .models import SystemSetting


class BootstrapMixin:
    """Adds Bootstrap 5 classes to every widget."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for field in self.fields.values():
            w = field.widget
            if isinstance(w, (forms.CheckboxInput,)):
                w.attrs.setdefault("class", "form-check-input")
            elif isinstance(w, forms.CheckboxSelectMultiple):
                pass
            elif isinstance(w, (forms.Select, forms.SelectMultiple)):
                w.attrs.setdefault("class", "form-select")
            else:
                w.attrs.setdefault("class", "form-control")


class SystemSettingForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = SystemSetting
        fields = [
            "company_name", "time_format", "default_shift", "auto_shift_detection", "auto_attendance_sync", "attendance_sync_interval",
            "auto_user_sync", "user_sync_interval", "push_on_employee_save", "device_timeout",
        ]

    def clean_attendance_sync_interval(self):
        v = self.cleaned_data["attendance_sync_interval"]
        if v < 1:
            raise forms.ValidationError("Minimum 1 minute.")
        return v

    def clean_user_sync_interval(self):
        v = self.cleaned_data["user_sync_interval"]
        if v < 5:
            raise forms.ValidationError("Minimum 5 minutes.")
        return v


class FirstRunForm(BootstrapMixin, forms.Form):
    """First-time setup: the main administrator account."""

    company_name = forms.CharField(label="Company name", max_length=120, initial="My Company")
    username = forms.CharField(label="Administrator username", max_length=150, initial="admin",
                               help_text="Letters, digits and @ . + - _ only.")
    email = forms.EmailField(label="Administrator email")
    password1 = forms.CharField(label="Password", widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}),
                                help_text="At least 8 characters; not a common password, not only numbers.")
    password2 = forms.CharField(label="Repeat password", widget=forms.PasswordInput(attrs={"autocomplete": "new-password"}))
    setup_code = forms.CharField(label="Setup code", required=False, max_length=64,
                                 help_text="Shown in the server log and in var/setup-code.txt "
                                           "(or run: manage.py setup_code).")

    def __init__(self, *args, code_required=True, **kwargs):
        super().__init__(*args, **kwargs)
        self.code_required = code_required
        if code_required:
            self.fields["setup_code"].required = True
        else:
            del self.fields["setup_code"]

    def clean_username(self):
        from django.contrib.auth import get_user_model
        from django.contrib.auth.validators import UnicodeUsernameValidator
        username = self.cleaned_data["username"].strip()
        UnicodeUsernameValidator()(username)
        if get_user_model().objects.filter(username__iexact=username).exists():
            raise forms.ValidationError("This username already exists.")
        return username

    def clean_setup_code(self):
        from .firstrun import check_setup_code
        code = self.cleaned_data.get("setup_code")
        if not check_setup_code(code):
            raise forms.ValidationError("The setup code is not correct.")
        return code

    def clean(self):
        data = super().clean()
        p1, p2 = data.get("password1"), data.get("password2")
        if p1 and p2 and p1 != p2:
            self.add_error("password2", "The passwords do not match.")
        elif p1 and data.get("username"):
            from django.contrib.auth import get_user_model
            from django.contrib.auth.password_validation import validate_password
            try:
                validate_password(p1, get_user_model()(username=data["username"], email=data.get("email", "")))
            except forms.ValidationError as exc:
                self.add_error("password1", exc)
        return data
