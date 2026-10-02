from django import forms

from core.forms import BootstrapMixin

from .models import BackupDestination, BackupSettings

KIND_FIELDS = {
    # field -> kinds that show it
    "host": "ftp ftps sftp", "port": "ftp ftps sftp", "username": "ftp ftps sftp webdav",
    "path": "local s3 ftp ftps sftp webdav rclone", "bucket": "s3", "region": "s3", "endpoint_url": "s3",
    "access_key": "s3", "url": "webdav", "passive": "ftp ftps", "verify_tls": "s3 ftps webdav",
    "password": "ftp ftps sftp webdav", "secret_key": "s3", "private_key": "sftp", "key_passphrase": "sftp",
    "rclone_remote": "rclone", "rclone_config": "rclone",
}


class DestinationForm(BootstrapMixin, forms.ModelForm):
    password = forms.CharField(widget=forms.PasswordInput(render_value=False), required=False)
    secret_key = forms.CharField(label="Secret access key", widget=forms.PasswordInput(render_value=False), required=False)
    private_key = forms.CharField(label="Private key (optional)", required=False, widget=forms.Textarea(attrs={
        "rows": 4, "placeholder": "-----BEGIN OPENSSH PRIVATE KEY-----", "class": "form-control mono"}),
        help_text="Use a key instead of (or together with) the password.")
    key_passphrase = forms.CharField(label="Key passphrase", widget=forms.PasswordInput(render_value=False), required=False)
    rclone_config = forms.CharField(label="rclone config", required=False, widget=forms.Textarea(attrs={
        "rows": 7, "class": "form-control mono", "placeholder": "[gdrive]\ntype = drive\nscope = drive.file\ntoken = {...}"}),
        help_text="Paste the section for your remote from rclone.conf (run 'rclone config' on any computer to create it; "
                  "'rclone config file' shows where it is). Stored encrypted.")

    class Meta:
        model = BackupDestination
        fields = ["name", "kind", "enabled", "host", "port", "username", "password", "url", "bucket", "region",
                  "endpoint_url", "access_key", "secret_key", "private_key", "key_passphrase",
                  "rclone_remote", "rclone_config", "path",
                  "passive", "verify_tls", "keep_last"]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        inst = self.instance
        if inst.pk:
            keep = "Leave empty to keep the saved value."
            if inst.secret_encrypted:
                self.fields["password"].help_text = keep
                self.fields["secret_key"].help_text = keep
            if inst.private_key_encrypted:
                self.fields["private_key"].help_text = "A key is saved. " + keep
            if inst.rclone_config_encrypted:
                self.fields["rclone_config"].help_text = "A config is saved (stored encrypted). " + keep
        self.fields["path"].help_text = ("Local: full folder path, e.g. /mnt/backup/bioattend. Other types: folder "
                                         "on the server / key prefix, e.g. backups/bioattend.")
        for name, field in self.fields.items():
            field.widget.attrs["data-kinds"] = KIND_FIELDS.get(name, "")

    def clean(self):
        data = super().clean()
        kind = data.get("kind")
        inst = self.instance
        need = {
            "local": ["path"], "s3": ["bucket", "access_key"], "ftp": ["host"], "ftps": ["host"],
            "sftp": ["host", "username"], "webdav": ["url"], "rclone": ["rclone_remote"],
        }.get(kind, [])
        for f in need:
            if not data.get(f):
                self.add_error(f, "Required for this type.")
        if kind == "s3" and not (data.get("secret_key") or inst.secret_encrypted):
            self.add_error("secret_key", "Required for S3.")
        if kind == "sftp" and not (data.get("password") or data.get("private_key") or inst.secret_encrypted
                                   or inst.private_key_encrypted):
            self.add_error("password", "Give a password or a private key.")
        if kind == "rclone":
            from .transports import REMOTE_NAME_RE
            remote = (data.get("rclone_remote") or "").strip().rstrip(":")
            data["rclone_remote"] = remote
            if remote and not REMOTE_NAME_RE.match(remote):
                self.add_error("rclone_remote", "Letters, digits, - _ . only (no colon).")
            config = data.get("rclone_config") or ""
            if not config and not inst.rclone_config_encrypted:
                self.add_error("rclone_config", "Paste the rclone config for this remote.")
            elif config and remote and f"[{remote}]" not in config:
                self.add_error("rclone_config", f"The config has no [{remote}] section.")
            for line in config.splitlines():
                key = line.split("=", 1)[0].strip().lower() if "=" in line else ""
                if key == "ssh" or "command" in key:
                    self.add_error("rclone_config", f"The option '{key}' is not allowed (it runs programs on the server).")
                    break
        return data

    def save(self, commit=True):
        obj = super().save(commit=False)
        data = self.cleaned_data
        if data.get("rclone_config"):
            obj.rclone_config = data["rclone_config"].strip() + "\n"
        secret = data.get("secret_key") if obj.kind == "s3" else data.get("password")
        if secret:
            obj.secret = secret
        if data.get("private_key"):
            obj.private_key = data["private_key"].strip() + "\n"
        if data.get("key_passphrase"):
            obj.key_passphrase = data["key_passphrase"]
        if "host" in self.changed_data or "port" in self.changed_data:
            obj.host_key = ""  # new server: learn its key again
        if commit:
            obj.save()
        return obj


class BackupSettingsForm(BootstrapMixin, forms.ModelForm):
    passphrase = forms.CharField(label="Encryption passphrase", required=False, min_length=8,
                                 widget=forms.PasswordInput(render_value=False))
    passphrase_confirm = forms.CharField(label="Repeat passphrase", required=False,
                                         widget=forms.PasswordInput(render_value=False))

    class Meta:
        model = BackupSettings
        fields = ["enabled", "frequency", "time_of_day", "weekday", "encrypt", "passphrase", "passphrase_confirm",
                  "include_logins"]
        widgets = {"time_of_day": forms.TimeInput(attrs={"type": "time"}, format="%H:%M")}

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.passphrase_encrypted:
            self.fields["passphrase"].help_text = ("A passphrase is saved. Leave empty to keep it. KEEP A COPY: "
                                                   "encrypted backups cannot be restored without it.")
        else:
            self.fields["passphrase"].help_text = "At least 8 characters. Keep a copy somewhere safe!"

    def clean(self):
        data = super().clean()
        p1, p2 = data.get("passphrase"), data.get("passphrase_confirm")
        if p1 and p1 != p2:
            self.add_error("passphrase_confirm", "The passphrases do not match.")
        if data.get("encrypt") and not (p1 or self.instance.passphrase_encrypted):
            self.add_error("passphrase", "Set a passphrase to encrypt backups (or turn encryption off).")
        return data

    def save(self, commit=True):
        obj = super().save(commit=False)
        if self.cleaned_data.get("passphrase"):
            obj.passphrase = self.cleaned_data["passphrase"]
        if commit:
            obj.save()
        return obj
