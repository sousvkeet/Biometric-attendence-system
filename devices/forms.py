from django import forms

from core.forms import BootstrapMixin

from .models import Device


class DeviceForm(BootstrapMixin, forms.ModelForm):
    class Meta:
        model = Device
        fields = ["name", "ip_address", "port", "comm_password", "driver", "area",
                  "is_active", "is_enroll_device", "live_enabled", "read_only"]

    def clean(self):
        data = super().clean()
        if data.get("driver") == Device.DRIVER_ZK and data.get("ip_address"):
            import ipaddress
            try:
                data["ip_address"] = str(ipaddress.ip_address(data["ip_address"].strip()))
            except ValueError:
                self.add_error("ip_address", "Enter the device's IP address, e.g. 192.168.1.201.")
        port = data.get("port")
        if port is not None and not 1 <= port <= 65535:
            self.add_error("port", "Port must be between 1 and 65535.")
        return data


class SimulatedPunchForm(BootstrapMixin, forms.Form):
    user_id = forms.CharField(max_length=24, label="User ID")
    when = forms.DateTimeField(required=False, widget=forms.DateTimeInput(attrs={"type": "datetime-local"}),
                               help_text="Empty = now")
