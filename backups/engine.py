"""Create, upload, encrypt/decrypt and restore database backups."""
import base64
import gzip
import hashlib
import logging
import os
import shutil
import sqlite3
import struct
import subprocess
import tempfile

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from django.conf import settings
from django.db import connection
from django.utils import timezone

from .models import BackupDestination, BackupRun, BackupSettings
from .transports import transport_for

logger = logging.getLogger("core")

MAGIC = b"BIOBK1"
CHUNK = 4 * 1024 * 1024
KDF_ITERATIONS = 390_000


class BackupError(Exception):
    pass


# ---------------------------------------------------------------- encryption (passphrase, chunked Fernet/AES)
def _key(passphrase, salt):
    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=KDF_ITERATIONS)
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode()))


def encrypt_file(src, dst, passphrase):
    salt = os.urandom(16)
    f = Fernet(_key(passphrase, salt))
    with open(src, "rb") as fin, open(dst, "wb") as fout:
        fout.write(MAGIC + salt)
        while chunk := fin.read(CHUNK):
            token = f.encrypt(chunk)
            fout.write(struct.pack(">I", len(token)) + token)


def decrypt_file(src, dst, passphrase):
    with open(src, "rb") as fin:
        if fin.read(len(MAGIC)) != MAGIC:
            raise BackupError("Not an encrypted BioAttend backup")
        f = Fernet(_key(passphrase, fin.read(16)))
        with open(dst, "wb") as fout:
            while header := fin.read(4):
                (length,) = struct.unpack(">I", header)
                try:
                    fout.write(f.decrypt(fin.read(length)))
                except InvalidToken:
                    raise BackupError("Wrong passphrase or damaged backup file")


def sha256_of(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------- creating the file
def _engine():
    return settings.DATABASES["default"]["ENGINE"]


def create_backup_file(workdir, passphrase=None, log=lambda m: None, include_logins=True):
    """Returns (path, name). The file name always matches transports.BACKUP_NAME_RE."""
    from .logins import NOLOGINS_TAG, strip_logins_sqlite
    stamp = timezone.localtime().strftime("%Y%m%d-%H%M%S")
    engine = _engine()
    if "sqlite3" in engine:
        raw = os.path.join(workdir, f"snapshot-{stamp}.sqlite3")
        if connection.in_atomic_block:
            raise BackupError("A backup cannot be taken inside an open database transaction")
        connection.ensure_connection()
        dst = sqlite3.connect(raw)
        try:
            connection.connection.backup(dst)  # consistent online copy while the portal keeps running
        finally:
            dst.close()
        check = sqlite3.connect(raw)
        try:
            result = check.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            check.close()
        if result != "ok":
            raise BackupError(f"Integrity check of the snapshot failed: {result}")
        log(f"Database snapshot taken ({os.path.getsize(raw) / 1e6:.1f} MB), integrity OK")
        tag = ""
        if not include_logins:
            strip_logins_sqlite(raw, log)
            tag = NOLOGINS_TAG
        name = f"bioattend-backup-{stamp}{tag}.sqlite3.gz"
        path = os.path.join(workdir, name)
        with open(raw, "rb") as fin, gzip.open(path, "wb", compresslevel=6) as fout:
            shutil.copyfileobj(fin, fout, CHUNK)
        os.remove(raw)
    elif "postgresql" in engine:
        db = settings.DATABASES["default"]
        name = f"bioattend-backup-{stamp}.pgdump"
        path = os.path.join(workdir, name)
        env = dict(os.environ, PGPASSWORD=str(db.get("PASSWORD") or ""))
        cmd = ["pg_dump", "--format=custom", "--no-owner", "-f", path, "-h", str(db.get("HOST") or "localhost"),
               "-p", str(db.get("PORT") or 5432), "-U", str(db.get("USER") or ""), str(db["NAME"])]
        proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
        if proc.returncode:
            raise BackupError(f"pg_dump failed: {proc.stderr.strip()[:300]}")
        log("PostgreSQL dump created")
        if not include_logins:
            log("Note: on PostgreSQL backups always include user accounts; restoring can still keep the current logins")
    else:
        raise BackupError(f"Unsupported database engine {engine}")
    log(f"Compressed backup: {os.path.getsize(path) / 1e6:.2f} MB")
    if passphrase:
        enc_path = path + ".enc"
        encrypt_file(path, enc_path, passphrase)
        os.remove(path)
        path, name = enc_path, name + ".enc"
        log("Encrypted with the backup passphrase (AES)")
    return path, name


# ---------------------------------------------------------------- a full run
def apply_retention(transport, keep, log):
    if not keep:
        return 0
    names = transport.list_backups()  # only files created by this system, oldest first (timestamp in name)
    removed = 0
    for name in names[:-keep] if len(names) > keep else []:
        transport.delete(name)
        removed += 1
    if removed:
        log(f"  retention: removed {removed} old backup(s), keeping the last {keep}")
    return removed


def run_backup(run, include_logins=None):
    """Execute a claimed BackupRun (status=running)."""
    cfg = BackupSettings.get()
    if include_logins is None:
        include_logins = cfg.include_logins
    destinations = list(BackupDestination.objects.filter(enabled=True))
    work = os.path.join(settings.BACKUP_WORK_DIR, "tmp")
    os.makedirs(work, exist_ok=True)
    workdir = tempfile.mkdtemp(dir=work)
    results, status = {}, "failed"
    try:
        if not destinations:
            raise BackupError("No enabled backup destinations. Add one first.")
        passphrase = cfg.passphrase if cfg.encrypt else None
        if cfg.encrypt and not passphrase:
            raise BackupError("Encryption is on but no passphrase is set (Backups → Schedule & encryption).")
        path, name = create_backup_file(workdir, passphrase, run.write, include_logins=include_logins)
        run.file_name, run.size, run.sha256, run.encrypted = name, os.path.getsize(path), sha256_of(path), bool(passphrase)
        run.save(update_fields=["file_name", "size", "sha256", "encrypted"])
        run.write(f"SHA-256 {run.sha256}")
        for d in destinations:
            run.write(f"Uploading to {d.name} ({d.get_kind_display().split(' (')[0]})...")
            try:
                t = transport_for(d)
                t.upload(path, name)
                apply_retention(t, d.keep_last, run.write)
                results[str(d.pk)] = {"name": d.name, "ok": True, "message": "Uploaded"}
                d.last_status, d.last_message, d.last_success_at = "ok", f"Backup {name}", timezone.now()
                run.write(f"  OK – {d.target_label}/{name}")
            except Exception as exc:
                logger.exception("Backup upload to %s failed", d)
                results[str(d.pk)] = {"name": d.name, "ok": False, "message": str(exc)[:300]}
                d.last_status, d.last_message = "error", str(exc)[:300]
                run.write(f"  FAILED – {exc}")
            d.save(update_fields=["last_status", "last_message", "last_success_at"])
        ok = sum(1 for r in results.values() if r["ok"])
        status = "success" if ok == len(destinations) else ("partial" if ok else "failed")
    except Exception as exc:
        logger.exception("Backup #%s failed", run.pk)
        run.write(f"FAILED: {exc}")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        run.status, run.results, run.finished_at = status, results, timezone.now()
        run.save(update_fields=["status", "results", "finished_at"])
        run.write(f"Finished: {run.get_status_display()}")
    return run


def backup_now(trigger="manual", user=None, include_logins=None):
    """Run a backup right away in this process (tray menu, command line). Raises BackupError when it cannot start."""
    cfg = BackupSettings.get()
    if not BackupDestination.objects.filter(enabled=True).exists():
        raise BackupError("No backup destination yet: add one under Backups in the portal.")
    if cfg.encrypt and not cfg.passphrase_encrypted:
        raise BackupError("Set an encryption passphrase first (Backups → Schedule & encryption).")
    if BackupRun.objects.filter(status__in=["pending", "running"]).exists():
        raise BackupError("A backup is already in progress.")
    run = BackupRun.objects.create(trigger=trigger, status="running", started_at=timezone.now(), created_by=user)
    return run_backup(run, include_logins=include_logins)


def claim_pending_run():
    for run in BackupRun.objects.filter(status="pending").order_by("created_at")[:3]:
        if BackupRun.objects.filter(pk=run.pk, status="pending").update(status="running", started_at=timezone.now()):
            run.refresh_from_db()
            return run
    return None


def schedule_due():
    """Create a scheduled run when the configured time has passed. Returns the run or None."""
    cfg = BackupSettings.get()
    if not cfg.enabled:
        return None
    now = timezone.now()
    if cfg.last_scheduled_at is None:
        BackupSettings.objects.filter(pk=1).update(last_scheduled_at=now)  # start counting from now
        return None
    if now >= cfg.next_run_after(cfg.last_scheduled_at):
        BackupSettings.objects.filter(pk=1).update(last_scheduled_at=now)
        if not BackupRun.objects.filter(status__in=["pending", "running"]).exists():
            return BackupRun.objects.create(trigger="scheduled")
    return None


# ---------------------------------------------------------------- restore
def prepare_restore_file(path, passphrase, workdir, log=print):
    """Decrypt/decompress a backup into a ready-to-restore file. Returns (kind, path)."""
    name = os.path.basename(path)
    if name.endswith(".enc"):
        if not passphrase:
            raise BackupError("This backup is encrypted: give the passphrase")
        out = os.path.join(workdir, name[:-4])
        decrypt_file(path, out, passphrase)
        path, name = out, name[:-4]
        log("Decrypted")
    if name.endswith(".sqlite3.gz") or name.endswith(".sqlite3"):
        out = os.path.join(workdir, "restore.sqlite3")
        if name.endswith(".gz"):
            with gzip.open(path, "rb") as fin, open(out, "wb") as fout:
                shutil.copyfileobj(fin, fout, CHUNK)
        else:  # an uncompressed safety copy
            shutil.copyfile(path, out)
        try:
            con = sqlite3.connect(out)
            try:
                if con.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise BackupError("The backup database failed its integrity check")
                tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            finally:
                con.close()
        except sqlite3.DatabaseError as exc:
            raise BackupError(f"The file is not a valid database ({exc})")
        if "employees_employee" not in tables:
            raise BackupError("This file is not a BioAttend database")
        log("Backup verified (integrity OK)")
        return "sqlite", out
    if name.endswith(".pgdump"):
        return "postgres", path
    raise BackupError(f"Unknown backup file type: {name}")


SAFETY_KEEP = 5


def safety_dir():
    path = os.path.join(settings.BACKUP_WORK_DIR, "pre-restore")
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path


def list_safety_copies():
    """Newest first: [(name, size, mtime)]."""
    folder = safety_dir()
    names = sorted((n for n in os.listdir(folder) if n.startswith("before-restore-") and n.endswith((".sqlite3", ".pgdump"))),
                   reverse=True)
    return [(n, os.path.getsize(os.path.join(folder, n)), os.path.getmtime(os.path.join(folder, n))) for n in names]


def restore_sqlite(prepared, log=print):
    """Replace the current SQLite database with `prepared`, after saving a safety copy. Uses Django's own
    connection so it works for the live file database; other processes must be paused (maintenance mode)."""
    if connection.in_atomic_block:
        raise BackupError("Cannot restore inside an open database transaction")
    safety = os.path.join(safety_dir(), f"before-restore-{timezone.localtime():%Y%m%d-%H%M%S}.sqlite3")
    connection.ensure_connection()
    live = connection.connection
    dst = sqlite3.connect(safety)
    try:
        live.backup(dst)
    finally:
        dst.close()
    os.chmod(safety, 0o600)  # unencrypted copy with personal and biometric data
    log("Safety copy of the current database saved")
    src = sqlite3.connect(prepared)
    try:
        src.backup(live)  # overwrite in place
    finally:
        src.close()
    connection.close()
    for name, _size, _mtime in list_safety_copies()[SAFETY_KEEP:]:
        os.remove(os.path.join(safety_dir(), name))
    log("Database restored")
    return safety


def restore_postgres(prepared, log=print):
    """pg_restore over the current database (after a safety dump)."""
    db = settings.DATABASES["default"]
    env = dict(os.environ, PGPASSWORD=str(db.get("PASSWORD") or ""))
    conn = ["-h", str(db.get("HOST") or "localhost"), "-p", str(db.get("PORT") or 5432), "-U", str(db.get("USER") or "")]
    safety = os.path.join(safety_dir(), f"before-restore-{timezone.localtime():%Y%m%d-%H%M%S}.pgdump")
    proc = subprocess.run(["pg_dump", "--format=custom", "--no-owner", "-f", safety, *conn, str(db["NAME"])],
                          env=env, capture_output=True, text=True)
    if proc.returncode:
        raise BackupError(f"Safety dump failed: {proc.stderr.strip()[:300]}")
    log("Safety copy of the current database saved")
    connection.close()
    _pg_restore(prepared, env, conn, db)
    log("Database restored")
    return safety


def _pg_restore(prepared, env, conn, db):
    cmd = ["pg_restore", "--clean", "--if-exists", "--no-owner", *conn, "-d", str(db["NAME"]), prepared]
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    if proc.returncode:
        raise BackupError(f"pg_restore failed: {proc.stderr.strip()[:500]}")
