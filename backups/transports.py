"""Backup destinations. Every transport offers: test(), upload(), list_backups(), delete(), download().

Only files whose names match BACKUP_NAME_RE (created by this system) are ever listed or deleted.
"""
import ftplib
import io
import os
import re
import shutil
import ssl
import uuid
import xml.etree.ElementTree as ET
from urllib.parse import quote, unquote, urlparse

BACKUP_NAME_RE = re.compile(r"^bioattend-backup-\d{8}-\d{6}(-nologins)?\.[a-z0-9.]+\Z")
TIMEOUT = 30


class TransportError(Exception):
    pass


def _clean(path):
    return (path or "").strip().strip("/")


class BaseTransport:
    def __init__(self, dest):
        self.d = dest

    def test(self):
        """Connect, write a small file, read the folder, delete the file."""
        name = f".bioattend-test-{uuid.uuid4().hex[:8]}"
        tmp = os.path.join(_tmpdir(), name)
        with open(tmp, "wb") as fh:
            fh.write(b"bioattend connection test")
        try:
            self._put(tmp, name)
            self._remove(name)
        finally:
            os.remove(tmp)
        return f"Connected and wrote a test file to {self.d.target_label}"

    def upload(self, local_path, name):
        self._put(local_path, name)

    def list_backups(self):
        return sorted(n for n in self._names() if BACKUP_NAME_RE.match(n))

    def delete(self, name):
        if not BACKUP_NAME_RE.match(name):
            raise TransportError(f"Refusing to delete '{name}': not a backup file created by this system")
        self._remove(name)

    def download(self, name, local_path):
        raise NotImplementedError

    # implemented by subclasses
    def _put(self, local_path, name):
        raise NotImplementedError

    def _names(self):
        raise NotImplementedError

    def _remove(self, name):
        raise NotImplementedError


def _tmpdir():
    from django.conf import settings
    path = os.path.join(settings.BACKUP_WORK_DIR, "tmp")
    os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------- local folder
class LocalTransport(BaseTransport):
    @property
    def folder(self):
        if not self.d.path:
            raise TransportError("Folder path is required")
        return os.path.abspath(os.path.expanduser(self.d.path))

    def _put(self, local_path, name):
        os.makedirs(self.folder, exist_ok=True)
        tmp = os.path.join(self.folder, name + ".part")
        shutil.copyfile(local_path, tmp)
        os.chmod(tmp, 0o600)
        os.replace(tmp, os.path.join(self.folder, name))

    def _names(self):
        return os.listdir(self.folder) if os.path.isdir(self.folder) else []

    def _remove(self, name):
        os.remove(os.path.join(self.folder, name))

    def download(self, name, local_path):
        shutil.copyfile(os.path.join(self.folder, name), local_path)

    def file_path(self, name):
        path = os.path.join(self.folder, name)
        return path if BACKUP_NAME_RE.match(name) and os.path.isfile(path) else None


# ---------------------------------------------------------------- S3 / S3-compatible
class S3Transport(BaseTransport):
    def _client(self):
        import boto3
        from botocore.config import Config
        if not (self.d.bucket and self.d.access_key and self.d.secret):
            raise TransportError("Bucket, access key and secret key are required")
        return boto3.client(
            "s3", endpoint_url=self.d.endpoint_url or None, region_name=self.d.region or None,
            aws_access_key_id=self.d.access_key, aws_secret_access_key=self.d.secret,
            verify=self.d.verify_tls,
            config=Config(connect_timeout=TIMEOUT, read_timeout=120, retries={"max_attempts": 3},
                          s3={"addressing_style": "path" if self.d.endpoint_url else "auto"}),
        )

    def _key(self, name):
        prefix = _clean(self.d.path)
        return f"{prefix}/{name}" if prefix else name

    def _put(self, local_path, name):
        self._client().upload_file(local_path, self.d.bucket, self._key(name))

    def _names(self):
        client, prefix = self._client(), _clean(self.d.path)
        names, token = [], None
        while True:
            kwargs = {"Bucket": self.d.bucket, "Prefix": f"{prefix}/" if prefix else ""}
            if token:
                kwargs["ContinuationToken"] = token
            resp = client.list_objects_v2(**kwargs)
            for obj in resp.get("Contents", []):
                rest = obj["Key"][len(kwargs["Prefix"]):]
                if "/" not in rest:
                    names.append(rest)
            if not resp.get("IsTruncated"):
                return names
            token = resp.get("NextContinuationToken")

    def _remove(self, name):
        self._client().delete_object(Bucket=self.d.bucket, Key=self._key(name))

    def download(self, name, local_path):
        self._client().download_file(self.d.bucket, self._key(name), local_path)


# ---------------------------------------------------------------- FTP / FTPS
class FTPTransport(BaseTransport):
    tls = False

    def _connect(self):
        if not self.d.host:
            raise TransportError("Host is required")
        if self.tls:
            ctx = ssl.create_default_context()
            if not self.d.verify_tls:
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
            ftp = ftplib.FTP_TLS(context=ctx, timeout=TIMEOUT)
        else:
            ftp = ftplib.FTP(timeout=TIMEOUT)
        ftp.connect(self.d.host, self.d.port or 21)
        ftp.login(self.d.username or "anonymous", self.d.secret or "")
        if self.tls:
            ftp.prot_p()
        ftp.set_pasv(self.d.passive)
        for part in _clean(self.d.path).split("/"):
            if not part:
                continue
            try:
                ftp.cwd(part)
            except ftplib.error_perm:
                ftp.mkd(part)
                ftp.cwd(part)
        return ftp

    def _put(self, local_path, name):
        ftp = self._connect()
        try:
            with open(local_path, "rb") as fh:
                ftp.storbinary(f"STOR {name}.part", fh)
            ftp.rename(f"{name}.part", name)
        finally:
            _quit(ftp)

    def _names(self):
        ftp = self._connect()
        try:
            try:
                return [n.rsplit("/", 1)[-1] for n in ftp.nlst()]
            except ftplib.error_perm:  # empty folder on some servers
                return []
        finally:
            _quit(ftp)

    def _remove(self, name):
        ftp = self._connect()
        try:
            ftp.delete(name)
        finally:
            _quit(ftp)

    def download(self, name, local_path):
        ftp = self._connect()
        try:
            with open(local_path, "wb") as fh:
                ftp.retrbinary(f"RETR {name}", fh.write)
        finally:
            _quit(ftp)


class FTPSTransport(FTPTransport):
    tls = True


def _quit(ftp):
    try:
        ftp.quit()
    except Exception:
        ftp.close()


# ---------------------------------------------------------------- SFTP
class SFTPTransport(BaseTransport):
    def _connect(self):
        import paramiko
        if not self.d.host:
            raise TransportError("Host is required")
        client = paramiko.SSHClient()
        port = self.d.port or 22
        host_id = self.d.host if port == 22 else f"[{self.d.host}]:{port}"
        if self.d.host_key:
            key_type, key_b64 = self.d.host_key.split(" ", 1)
            client.get_host_keys().add(host_id, key_type, paramiko.PKey.from_type_string(key_type, _b64(key_b64)))
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:  # trust on first use: remember the server key
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        pkey = self._pkey()
        try:
            client.connect(self.d.host, port=port, username=self.d.username, password=self.d.secret or None,
                           pkey=pkey, timeout=TIMEOUT, banner_timeout=TIMEOUT, auth_timeout=TIMEOUT,
                           look_for_keys=False, allow_agent=False)
        except paramiko.BadHostKeyException as exc:
            raise TransportError("The SFTP server's key has CHANGED since it was first seen. If this is expected, "
                                 "click 'Forget server key' on the destination; otherwise do not trust this server.") from exc
        if not self.d.host_key:
            key = client.get_transport().get_remote_server_key()
            self.d.host_key = f"{key.get_name()} {key.get_base64()}"
            if self.d.pk:
                type(self.d).objects.filter(pk=self.d.pk).update(host_key=self.d.host_key)
        sftp = client.open_sftp()
        folder = _clean(self.d.path)
        if folder:
            current = "/" if self.d.path.strip().startswith("/") else ""
            for part in folder.split("/"):
                current = f"{current}{part}/"
                try:
                    sftp.stat(current)
                except FileNotFoundError:
                    sftp.mkdir(current)
            sftp.chdir(current)
        return client, sftp

    def _pkey(self):
        import paramiko
        text = self.d.private_key
        if not text:
            return None
        for cls in (paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey):
            try:
                return cls.from_private_key(io.StringIO(text), password=self.d.key_passphrase or None)
            except (paramiko.SSHException, ValueError):
                continue
        raise TransportError("Could not read the private key (wrong format or passphrase)")

    def _put(self, local_path, name):
        client, sftp = self._connect()
        try:
            sftp.put(local_path, name + ".part")
            try:
                sftp.posix_rename(name + ".part", name)
            except OSError:
                sftp.rename(name + ".part", name)
        finally:
            client.close()

    def _names(self):
        client, sftp = self._connect()
        try:
            return sftp.listdir()
        finally:
            client.close()

    def _remove(self, name):
        client, sftp = self._connect()
        try:
            sftp.remove(name)
        finally:
            client.close()

    def download(self, name, local_path):
        client, sftp = self._connect()
        try:
            sftp.get(name, local_path)
        finally:
            client.close()


def _b64(text):
    import base64
    return base64.b64decode(text)


# ---------------------------------------------------------------- WebDAV
class WebDAVTransport(BaseTransport):
    def _session(self):
        import requests
        if not self.d.url:
            raise TransportError("WebDAV URL is required")
        s = requests.Session()
        if self.d.username:
            s.auth = (self.d.username, self.d.secret)
        s.verify = self.d.verify_tls
        return s

    def _base(self):
        base = self.d.url.rstrip("/") + "/"
        for part in _clean(self.d.path).split("/"):
            if part:
                base += quote(part) + "/"
        return base

    def _ensure_folder(self, s):
        url = self.d.url.rstrip("/") + "/"
        for part in _clean(self.d.path).split("/"):
            if not part:
                continue
            url += quote(part) + "/"
            r = s.request("MKCOL", url, timeout=TIMEOUT)
            if r.status_code not in (201, 405, 301, 302):  # 405 = already exists
                _check(r, "create folder")

    def _put(self, local_path, name):
        s = self._session()
        self._ensure_folder(s)
        with open(local_path, "rb") as fh:
            _check(s.put(self._base() + quote(name), data=fh, timeout=600), "upload")

    def _names(self):
        s = self._session()
        r = s.request("PROPFIND", self._base(), headers={"Depth": "1"}, timeout=TIMEOUT)
        if r.status_code == 404:
            return []
        _check(r, "list folder")
        names = []
        for href in ET.fromstring(r.content).iter("{DAV:}href"):
            path = unquote(urlparse(href.text or "").path).rstrip("/")
            names.append(path.rsplit("/", 1)[-1])
        return names

    def _remove(self, name):
        _check(self._session().delete(self._base() + quote(name), timeout=TIMEOUT), "delete")

    def download(self, name, local_path):
        r = self._session().get(self._base() + quote(name), stream=True, timeout=600)
        _check(r, "download")
        with open(local_path, "wb") as fh:
            for chunk in r.iter_content(1024 * 1024):
                fh.write(chunk)


def _check(response, action):
    if response.status_code >= 400:
        raise TransportError(f"WebDAV {action} failed: HTTP {response.status_code} {response.reason}")


# ---------------------------------------------------------------- rclone (70+ providers)
RCLONE_VERSION = "1.75.1"


def rclone_binary(log=lambda m: None):
    """Path of the rclone program. If it is not installed, the official build is downloaded once (checksum
    verified) into the data folder - so installations that never use rclone do not carry its 80 MB."""
    import platform
    import shutil
    from django.conf import settings
    configured = settings.RCLONE_BINARY
    if os.path.isabs(configured) and os.path.isfile(configured):
        return configured
    found = shutil.which(configured)
    if found:
        return found
    exe = "rclone.exe" if os.name == "nt" else "rclone"
    local = os.path.join(settings.VAR_DIR, "bin", exe)
    if os.path.isfile(local):
        return local
    arch = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
    system = {"nt": "windows"}.get(os.name, "linux" if platform.system() == "Linux" else None)
    if not arch or not system:
        raise TransportError("rclone is not installed and cannot be downloaded automatically for this system")
    log(f"rclone is not installed yet - downloading the official rclone {RCLONE_VERSION} (one time)...")
    import hashlib
    import io
    import zipfile

    import requests
    base = f"https://downloads.rclone.org/v{RCLONE_VERSION}"
    name = f"rclone-v{RCLONE_VERSION}-{system}-{arch}.zip"
    try:
        sums = requests.get(f"{base}/SHA256SUMS", timeout=30).text
        data = requests.get(f"{base}/{name}", timeout=600).content
    except requests.RequestException as exc:
        raise TransportError(f"rclone could not be downloaded: {exc}")
    expected = next((ln.split()[0] for ln in sums.splitlines() if ln.strip().endswith(name)), None)
    if not expected or hashlib.sha256(data).hexdigest() != expected:
        raise TransportError("rclone download failed its checksum check - not used")
    member = next(n for n in zipfile.ZipFile(io.BytesIO(data)).namelist() if n.endswith("/" + exe))
    os.makedirs(os.path.dirname(local), exist_ok=True)
    tmp = local + ".part"
    with open(tmp, "wb") as fh:
        fh.write(zipfile.ZipFile(io.BytesIO(data)).read(member))
    os.chmod(tmp, 0o755)
    os.replace(tmp, local)
    log(f"rclone {RCLONE_VERSION} installed in {os.path.dirname(local)}")
    return local


REMOTE_NAME_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_\-. ]*$")


class RcloneTransport(BaseTransport):
    """Runs the rclone binary. The rclone config (with tokens) is stored encrypted in the portal and written to a
    private temporary file only while rclone runs; tokens rclone refreshes are saved back."""

    def _remote(self, name=""):
        remote = (self.d.rclone_remote or "").strip().rstrip(":")
        if not REMOTE_NAME_RE.match(remote):
            raise TransportError("Enter the rclone remote name (letters, digits, - _ . only), e.g. gdrive")
        folder = _clean(self.d.path)
        target = f"{folder}/{name}" if folder and name else (folder or name)
        return f"{remote}:{target}"

    def _run(self, *args, timeout=900):
        import subprocess
        import tempfile
        from django.conf import settings
        config = self.d.rclone_config
        fd, conf_path = tempfile.mkstemp(prefix="rclone-", suffix=".conf", dir=_tmpdir())
        try:
            with os.fdopen(fd, "w") as fh:
                fh.write(config or "")
            os.chmod(conf_path, 0o600)
            cmd = [rclone_binary(), "--config", conf_path, "--retries", "3", "--low-level-retries", "5",
                   "--contimeout", f"{TIMEOUT}s", "--timeout", "300s", *args]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            except FileNotFoundError:
                raise TransportError("rclone could not be started")
            except subprocess.TimeoutExpired:
                raise TransportError("rclone timed out")
            with open(conf_path) as fh:
                updated = fh.read()
            if config and updated.strip() and updated != config and self.d.pk:  # refreshed OAuth token
                self.d.rclone_config = updated
                type(self.d).objects.filter(pk=self.d.pk).update(rclone_config_encrypted=self.d.rclone_config_encrypted)
            if proc.returncode:
                lines = [ln for ln in proc.stderr.strip().splitlines() if ln.strip()]
                raise TransportError("rclone: " + (lines[-1] if lines else f"exit code {proc.returncode}")[:300])
            return proc.stdout
        finally:
            try:
                os.remove(conf_path)
            except OSError:
                pass

    def _put(self, local_path, name):
        self._run("copyto", local_path, self._remote(name))

    def _names(self):
        try:
            out = self._run("lsf", "--files-only", "--max-depth", "1", self._remote(), timeout=120)
        except TransportError as exc:
            if "directory not found" in str(exc).lower():
                return []
            raise
        return [line.strip() for line in out.splitlines() if line.strip()]

    def _remove(self, name):
        self._run("deletefile", self._remote(name), timeout=120)

    def download(self, name, local_path):
        self._run("copyto", self._remote(name), local_path)


TRANSPORTS = {"rclone": RcloneTransport, "local": LocalTransport, "s3": S3Transport, "ftp": FTPTransport, "ftps": FTPSTransport,
              "sftp": SFTPTransport, "webdav": WebDAVTransport}


def transport_for(dest):
    return TRANSPORTS[dest.kind](dest)
