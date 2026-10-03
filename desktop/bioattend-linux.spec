# PyInstaller spec for BioAttend for Linux (run from the project root, normally by desktop/build_linux.sh):
#   pyinstaller desktop/bioattend-linux.spec --noconfirm
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = SPECPATH + "/.."
APPS = ["config", "core", "employees", "devices", "attendance", "backups"]
LIBS = ["django", "whitenoise", "waitress", "dj_database_url", "dotenv", "openpyxl", "zk", "boto3", "botocore",
        "s3transfer", "paramiko", "cryptography", "requests", "tzlocal", "tzdata"]


def keep(name):
    if name.rsplit(".", 1)[-1].startswith("test"):  # never ship test modules (tests, test_*.py)
        return False
    return not any(part in name for part in (".tests", ".test.", "django.contrib.gis", "django.db.backends.oracle",
                                             "django.db.backends.mysql"))


hidden = ["linux_desktop"]
for pkg in APPS + LIBS:
    hidden += collect_submodules(pkg, filter=keep)

datas = [
    (ROOT + "/templates", "templates"),
    (ROOT + "/static", "static"),
    (ROOT + "/staticfiles", "staticfiles"),
    (ROOT + "/desktop/bioattend.png", "desktop"),
]
BOTOCORE_SERVICES = {"s3", "sts"}  # backups only use S3 (STS for credentials)


def wanted(src):
    """Leave out files the app never uses (smaller download, faster start)."""
    path = src.replace("\\", "/")
    if "/locale/" in path:  # English-only app: drop all other translations
        return "/locale/en/" in path and not path.endswith(".po")
    if "/botocore/data/" in path:
        rest = path.split("/botocore/data/", 1)[1]
        return "/" not in rest or rest.split("/", 1)[0] in BOTOCORE_SERVICES
    return True


for pkg in ("django", "botocore", "tzdata"):
    datas += [(src, dest) for src, dest in collect_data_files(pkg) if wanted(src)]

a = Analysis(
    [ROOT + "/desktop/bioattend_desktop.py"],
    pathex=[ROOT, ROOT + "/desktop"],
    binaries=[],  # rclone is downloaded on first use (backups/transports.py: rclone_binary)
    datas=datas,
    hiddenimports=hidden,
    # No WebView/tray on Linux (see desktop/linux_desktop.py); PIL only draws the icon at build time.
    excludes=["tkinter", "gunicorn", "psycopg", "psycopg_binary", "pytest", "IPython", "webview", "pystray", "PIL"],
    noarchive=False,
)
a.datas = [entry for entry in a.datas if wanted(entry[1]) and wanted(entry[0])]
# The GCC runtime is part of every Linux system; the build system's copy needs a newer glibc than the app.
a.binaries = [entry for entry in a.binaries if not entry[0].startswith("libgcc_s.so")]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="bioattend", console=True, strip=True)
COLLECT(exe, a.binaries, a.datas, strip=True, name="bioattend")
