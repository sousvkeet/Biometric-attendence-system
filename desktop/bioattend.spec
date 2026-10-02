# PyInstaller spec for BioAttend for Windows (run from the project root):
#   pyinstaller desktop/bioattend.spec --noconfirm
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = SPECPATH + "/.."
APPS = ["config", "core", "employees", "devices", "attendance", "backups"]
LIBS = ["django", "whitenoise", "waitress", "dj_database_url", "dotenv", "openpyxl", "zk", "boto3", "botocore",
        "s3transfer", "paramiko", "cryptography", "requests", "webview", "pystray", "tzlocal", "tzdata"]


def keep(name):
    if name.rsplit(".", 1)[-1].startswith("test"):  # never ship test modules (tests, test_*.py)
        return False
    return not any(part in name for part in (".tests", ".test.", "django.contrib.gis", "django.db.backends.oracle",
                                             "django.db.backends.mysql", "webview.platforms.android",
                                             "webview.platforms.cocoa", "webview.platforms.gtk", "webview.platforms.qt"))


hidden = []
for pkg in APPS + LIBS:
    hidden += collect_submodules(pkg, filter=keep)

datas = [
    (ROOT + "/templates", "templates"),
    (ROOT + "/static", "static"),
    (ROOT + "/staticfiles", "staticfiles"),
    (ROOT + "/desktop/bioattend.ico", "desktop"),
    (ROOT + "/desktop/bioattend.png", "desktop"),
]
BOTOCORE_SERVICES = {"s3", "sts"}  # backups only use S3 (STS for credentials)


def wanted(src):
    """Leave out files the app never uses - fewer files means a much faster (MSI) installation."""
    path = src.replace("\\", "/")
    if "/locale/" in path:  # English-only app: drop all other translations
        return "/locale/en/" in path and not path.endswith(".po")
    if "/botocore/data/" in path:
        rest = path.split("/botocore/data/", 1)[1]
        return "/" not in rest or rest.split("/", 1)[0] in BOTOCORE_SERVICES
    return True


for pkg in ("django", "botocore", "webview", "tzdata"):
    datas += [(src, dest) for src, dest in collect_data_files(pkg) if wanted(src)]
binaries = []  # rclone is downloaded on first use (backups/transports.py: rclone_binary)

a = Analysis(
    [ROOT + "/desktop/bioattend_desktop.py"],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "gunicorn", "psycopg", "psycopg_binary", "pytest", "IPython",
              "PIL._avif", "PIL.AvifImagePlugin", "PIL.ImageQt", "PIL.ImageTk"],
    noarchive=False,
)
# PyInstaller's own Django/botocore hooks add their data files too: filter the final list as well
a.datas = [entry for entry in a.datas if wanted(entry[1]) and wanted(entry[0])]
a.binaries = [entry for entry in a.binaries if "_avif" not in entry[0]]
pyz = PYZ(a.pure)

app_exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="BioAttend", console=False,
              icon=ROOT + "/desktop/bioattend.ico", version=ROOT + "/desktop/version_info.txt")
cli_exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="bioattend-cli", console=True,
              icon=ROOT + "/desktop/bioattend.ico", version=ROOT + "/desktop/version_info.txt")
COLLECT(app_exe, cli_exe, a.binaries, a.datas, name="BioAttend")
