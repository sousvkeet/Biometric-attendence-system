"""First-time setup: until an administrator exists, every page leads to the setup page.

Security: on a web server anyone who reaches a fresh installation could otherwise claim the administrator account,
so the setup page also asks for a one-time *setup code* (written to the log and to var/setup-code.txt, readable only
by the server's user; `manage.py setup_code` prints it). The Windows desktop app only accepts setup from the PC
itself (loopback), where no code is needed.
"""
import datetime as dt
import ipaddress
import logging
import os
import secrets

from django.conf import settings
from django.contrib.auth import get_user_model

logger = logging.getLogger("core")


def setup_needed():
    return not get_user_model().objects.filter(is_superuser=True, is_active=True).exists()


def create_defaults(company=None):
    """Default shift, leave types and settings (safe to call repeatedly)."""
    from attendance.models import LeaveType, Shift
    from core.models import SystemSetting
    shift, _ = Shift.objects.get_or_create(
        name="General", defaults=dict(start_time=dt.time(9, 0), end_time=dt.time(18, 0), weekly_offs="6"))
    s = SystemSetting.get()
    if not s.default_shift_id:
        s.default_shift = shift
    if company:
        s.company_name = company
    s.save()
    for name, code, quota, paid in [("Casual leave", "CL", 12, True), ("Sick leave", "SL", 12, True),
                                    ("Earned leave", "EL", 15, True), ("Loss of pay", "LOP", 0, False)]:
        LeaveType.objects.get_or_create(code=code, defaults=dict(name=name, annual_quota=quota, is_paid=paid))
    return shift


# ---------------------------------------------------------------- setup code
def _code_path():
    os.makedirs(settings.VAR_DIR, exist_ok=True)
    return os.path.join(settings.VAR_DIR, "setup-code.txt")


def get_setup_code():
    """The current one-time setup code (created on first use)."""
    path = _code_path()
    try:
        with open(path, encoding="utf-8") as fh:
            code = fh.read().strip()
        if code:
            return code
    except FileNotFoundError:
        pass
    code = "-".join(secrets.token_hex(3).upper() for _ in range(4))  # e.g. 3F9A1C-0B7D2E-...
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(code + "\n")
    logger.warning("First-time setup: open the portal and enter this setup code: %s  (also in %s, or run "
                   "'manage.py setup_code')", code, path)
    return code


def check_setup_code(value):
    expected = get_setup_code().replace("-", "")
    given = (value or "").strip().upper().replace("-", "").replace(" ", "")
    return bool(given) and secrets.compare_digest(given, expected)


def delete_setup_code():
    try:
        os.remove(_code_path())
    except FileNotFoundError:
        pass


def code_required(request):
    """The desktop app trusts requests from the PC itself; everything else needs the setup code."""
    if getattr(settings, "DESKTOP_MODE", False):
        try:
            return not ipaddress.ip_address(request.META.get("REMOTE_ADDR", "")).is_loopback
        except ValueError:
            return True
    return True


class FirstRunMiddleware:
    EXEMPT = ("/setup/", "/static/", "/healthz", "/backups/restore/status.json")

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if not request.path.startswith(self.EXEMPT) and setup_needed():
            from django.shortcuts import redirect
            return redirect("first_run")
        return self.get_response(request)
