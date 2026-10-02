import datetime as dt
import os
import tempfile

from django.conf import settings
from django.contrib import messages
from django.http import FileResponse, Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST

from core.decorators import superuser_required
from core.models import ActivityLog, SystemSetting
from core.pagination import paginate

from . import maintenance
from .engine import list_safety_copies
from .logins import is_nologins_backup
from .forms import KIND_FIELDS, BackupSettingsForm, DestinationForm
from .models import BackupDestination, BackupRun, BackupSettings
from .transports import BACKUP_NAME_RE, LocalTransport, transport_for


@superuser_required
def index(request):
    cfg = BackupSettings.get()
    form = BackupSettingsForm(request.POST or None, instance=cfg)
    if request.method == "POST" and form.is_valid():
        form.save()
        ActivityLog.log(request, "Backup schedule updated")
        messages.success(request, "Backup settings saved.")
        return redirect("backups:index")
    next_run = None
    if cfg.enabled:
        next_run = cfg.next_run_after(cfg.last_scheduled_at or timezone.now())
    return render(request, "backups/index.html", {
        "form": form, "cfg": cfg, "next_run": next_run,
        "destinations": BackupDestination.objects.all(),
        "page": paginate(request, BackupRun.objects.select_related("created_by"), 20),
        "busy": BackupRun.objects.filter(status__in=["pending", "running"]).first(),
        "needs_passphrase": cfg.encrypt and not cfg.passphrase_encrypted,
        "worker_online": SystemSetting.get().worker_online,
        "kinds": BackupDestination.KIND_CHOICES,
        "safety_copies": [(n, size, dt.datetime.fromtimestamp(m, tz=dt.timezone.utc)) for n, size, m in list_safety_copies()],
        "last_restore": maintenance.read_status() if maintenance.read_status().get("finished") else None,
    })


@superuser_required
@require_POST
def backup_now(request):
    busy = BackupRun.objects.filter(status__in=["pending", "running"]).first()
    if busy:
        messages.info(request, "A backup is already in progress.")
        return redirect(busy)
    if not BackupDestination.objects.filter(enabled=True).exists():
        messages.error(request, "Add a backup destination first.")
        return redirect("backups:index")
    cfg = BackupSettings.get()
    if cfg.encrypt and not cfg.passphrase_encrypted:
        messages.error(request, "Set an encryption passphrase first (Schedule & encryption, on the right), "
                                "or untick “Encrypt backup files”. Nothing was backed up.")
        return redirect(reverse("backups:index") + "#schedule")
    run = BackupRun.objects.create(trigger="manual", created_by=request.user)
    ActivityLog.log(request, "Backup started")
    return redirect(run)


@superuser_required
def run_detail(request, pk):
    return render(request, "backups/run_detail.html", {"run": get_object_or_404(BackupRun, pk=pk),
                                                        "worker_online": SystemSetting.get().worker_online})


@superuser_required
def run_status(request, pk):
    run = get_object_or_404(BackupRun, pk=pk)
    return JsonResponse({"status": run.status, "status_label": run.get_status_display(), "log": run.log,
                         "finished": run.is_finished})


@superuser_required
def destination_form(request, pk=None):
    dest = get_object_or_404(BackupDestination, pk=pk) if pk else None
    local_default = str(settings.BASE_DIR / "var" / "backups" / "local")
    initial = None
    if not pk:
        kind = request.GET.get("kind", "local")
        initial = {"kind": kind, "path": local_default if kind == "local" else ""}
    form = DestinationForm(request.POST or None, instance=dest, initial=initial)
    if request.method == "POST" and form.is_valid():
        dest = form.save()
        ActivityLog.log(request, "Backup destination " + ("updated" if pk else "added"), dest, dest.get_kind_display())
        messages.success(request, f"Destination '{dest.name}' saved. Use “Test” to check the connection.")
        return redirect("backups:index")
    return render(request, "backups/destination_form.html", {"form": form, "dest": dest, "kind_fields": KIND_FIELDS,
                                                             "local_default": local_default})


@superuser_required
@require_POST
def destination_test(request, pk):
    dest = get_object_or_404(BackupDestination, pk=pk)
    try:
        msg = transport_for(dest).test()
        dest.last_status, dest.last_message = "ok", "Connection test passed"
        messages.success(request, f"{dest.name}: {msg}.")
    except Exception as exc:
        dest.last_status, dest.last_message = "error", f"Test failed: {exc}"[:300]
        messages.error(request, f"{dest.name}: connection test failed – {exc}")
    dest.save(update_fields=["last_status", "last_message"])
    return redirect("backups:index")


@superuser_required
@require_POST
def destination_delete(request, pk):
    dest = get_object_or_404(BackupDestination, pk=pk)
    ActivityLog.log(request, "Backup destination removed", dest)
    dest.delete()
    messages.success(request, f"Destination '{dest.name}' removed from the portal. Backup files already stored there were not deleted.")
    return redirect("backups:index")


@superuser_required
@require_POST
def destination_forget_key(request, pk):
    BackupDestination.objects.filter(pk=pk).update(host_key="")
    messages.info(request, "Server key forgotten; it will be learned again on the next connection.")
    return redirect("backups:destination_edit", pk=pk)


@superuser_required
def destination_files(request, pk):
    dest = get_object_or_404(BackupDestination, pk=pk)
    files, error = [], ""
    try:
        files = list(reversed(transport_for(dest).list_backups()))
    except Exception as exc:
        error = str(exc)
    return render(request, "backups/files.html", {"dest": dest, "files": files, "error": error})


class _TempFile:
    """File object that deletes itself when the download finishes."""

    def __init__(self, path):
        self.path, self.fh = path, open(path, "rb")

    def read(self, *a):
        return self.fh.read(*a)

    def close(self):
        self.fh.close()
        try:
            os.remove(self.path)
        except OSError:
            pass


@superuser_required
def destination_download(request, pk, name):
    dest = get_object_or_404(BackupDestination, pk=pk)
    if not BACKUP_NAME_RE.match(name):
        raise Http404
    ActivityLog.log(request, "Backup downloaded", name, dest.name)
    transport = transport_for(dest)
    if isinstance(transport, LocalTransport):
        path = transport.file_path(name)
        if not path:
            raise Http404
        return FileResponse(open(path, "rb"), as_attachment=True, filename=name)
    tmp_dir = os.path.join(settings.BACKUP_WORK_DIR, "tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=tmp_dir)
    os.close(fd)
    try:
        transport.download(name, tmp)
    except Exception as exc:
        os.remove(tmp)
        messages.error(request, f"Download failed: {exc}")
        return redirect("backups:files", pk=pk)
    return FileResponse(_TempFile(tmp), as_attachment=True, filename=name)


# ---------------------------------------------------------------- restore
def _restore_source(request):
    """Describe what is being restored from the query/form parameters, or None."""
    from .engine import list_safety_copies
    from .restore import UPLOAD_EXTENSIONS, uploads_dir
    p = request.POST if request.method == "POST" else request.GET
    if p.get("dest") and p.get("file"):
        dest = get_object_or_404(BackupDestination, pk=p["dest"])
        name = p["file"]
        if not BACKUP_NAME_RE.match(name):
            raise Http404
        return {"source": "destination", "dest": dest.pk, "file": name, "label": f"{name} from {dest.name}",
                "encrypted": name.endswith(".enc"), "params": {"dest": dest.pk, "file": name}}
    if p.get("upload"):
        name = os.path.basename(p["upload"])
        if not name.endswith(UPLOAD_EXTENSIONS) or not os.path.isfile(os.path.join(uploads_dir(), name)):
            raise Http404
        return {"source": "upload", "file": name, "label": f"uploaded file {name.split('__', 1)[-1]}",
                "encrypted": name.endswith(".enc"), "params": {"upload": name}}
    if p.get("safety"):
        name = os.path.basename(p["safety"])
        if name not in {n for n, _s, _m in list_safety_copies()}:
            raise Http404
        return {"source": "safety", "file": name, "label": f"safety copy {name}", "encrypted": False,
                "params": {"safety": name}}
    return None


@superuser_required
def restore_confirm(request):
    from .engine import BackupError
    from .restore import start
    src = _restore_source(request)
    if src is None:
        raise Http404
    can_backup_first = BackupDestination.objects.filter(enabled=True).exists() and src["source"] != "safety"
    errors = []
    if request.method == "POST":
        if not request.user.check_password(request.POST.get("password", "")):
            errors.append("Your admin password is not correct.")
        if request.POST.get("confirm", "").strip() != "RESTORE":
            errors.append("Type RESTORE to confirm.")
        if src["encrypted"] and not (request.POST.get("passphrase") or BackupSettings.get().passphrase):
            errors.append("This backup is encrypted: enter its passphrase.")
        if not errors:
            job = {k: src[k] for k in ("source", "file", "label") if k in src}
            job.update(dest=src.get("dest"), user=request.user.get_username(),
                       backup_first=can_backup_first and request.POST.get("backup_first") == "on",
                       keep_logins=request.POST.get("keep_logins") == "on")
            ActivityLog.log(request, "Database restore started", src["label"])
            try:
                start(job, request.POST.get("passphrase", ""))
            except BackupError as exc:
                errors.append(str(exc))
            else:
                return redirect("backups:index")  # the maintenance screen takes over
    return render(request, "backups/restore_confirm.html", {
        "src": src, "errors": errors, "can_backup_first": can_backup_first,
        "nologins": is_nologins_backup(src["file"]),
        "has_saved_passphrase": bool(BackupSettings.get().passphrase_encrypted),
    })


@superuser_required
@require_POST
def restore_upload(request):
    from .restore import UPLOAD_EXTENSIONS, uploads_dir
    f = request.FILES.get("backup_file")
    if not f:
        messages.error(request, "Choose a backup file.")
        return redirect("backups:index")
    if f.size > settings.BACKUP_UPLOAD_MAX_BYTES:
        messages.error(request, "The file is too large for a backup of this portal.")
        return redirect("backups:index")
    original = os.path.basename(f.name)
    if not original.endswith(UPLOAD_EXTENSIONS):
        messages.error(request, "Not a BioAttend backup file (.sqlite3.gz, .sqlite3.gz.enc, .pgdump, .pgdump.enc).")
        return redirect("backups:index")
    for old in os.listdir(uploads_dir()):  # only keep the latest upload
        os.remove(os.path.join(uploads_dir(), old))
    safe = "".join(c if c.isalnum() or c in "-._" else "_" for c in original)
    name = f"{timezone.localtime():%Y%m%d%H%M%S}__{safe}"
    fd = os.open(os.path.join(uploads_dir(), name), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as out:
        for chunk in f.chunks():
            out.write(chunk)
    from django.urls import reverse
    from urllib.parse import urlencode
    return redirect(reverse("backups:restore") + "?" + urlencode({"upload": name}))
