from django.http import HttpResponse, JsonResponse
from django.template.loader import render_to_string

from . import maintenance

STATUS_URL = "/backups/restore/status.json"


class MaintenanceMiddleware:
    """While a restore runs, every page shows the progress screen (no database access at all)."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == STATUS_URL:
            return JsonResponse(public_status())
        if maintenance.is_active() and not request.path.startswith("/static/"):
            html = render_to_string("backups/maintenance.html", {"status_url": STATUS_URL})
            resp = HttpResponse(html, status=503)
            resp["Retry-After"] = "10"
            resp._has_been_logged = True  # planned maintenance, not a server error (no error log / e-mail)
            return resp
        return self.get_response(request)


def public_status():
    """Progress only – no file paths or credentials (this endpoint needs no login)."""
    from .restore import STEPS
    import time
    s = maintenance.read_status()
    active = maintenance.is_active()
    recent = time.time() - (s.get("finished_at") or 0) < 15 * 60
    if not active and not (s.get("finished") and recent):
        return {"active": False}
    if s.get("finished"):
        if not s.get("ok"):
            message = "The restore did not complete. Sign in and open Backups for the details."
        elif s.get("logins_kept"):
            message = "Restore complete. User accounts and passwords were kept."
        else:
            message = "Restore complete. Sign in again with the password that was valid when the backup was made."
    else:
        message = dict(STEPS).get(s.get("stage", ""), "Working") + "…"
    return {"active": active, "finished": bool(s.get("finished")), "ok": s.get("ok"), "stage": s.get("stage", ""),
            "message": message, "backup_first": s.get("backup_first", False), "steps": STEPS}
