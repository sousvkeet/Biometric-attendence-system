# PyInstaller spec for BioAttend for Windows (run from the project root):
#   pyinstaller desktop/bioattend.spec --noconfirm
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = SPECPATH + "/.."
APPS = ["config", "core", "employees", "devices", "attendance", "backups"]
LIBS = ["django", "whitenoise", "waitress", "dj_database_url", "dotenv", "openpyxl", "zk", "boto3", "botocore",
        "s3transfer", "paramiko", "cryptography", "requests", "webview", "pystray", "tzlocal", "tzdata"]


def keep(name):
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
for pkg in ("django", "botocore", "webview", "tzdata"):
    datas += collect_data_files(pkg)
binaries = [(ROOT + "/desktop/vendor/rclone.exe", ".")]

a = Analysis(
    [ROOT + "/desktop/bioattend_desktop.py"],
    pathex=[ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "gunicorn", "psycopg", "psycopg_binary", "pytest", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)

app_exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="BioAttend", console=False,
              icon=ROOT + "/desktop/bioattend.ico", version=ROOT + "/desktop/version_info.txt")
cli_exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="bioattend-cli", console=True,
              icon=ROOT + "/desktop/bioattend.ico", version=ROOT + "/desktop/version_info.txt")
COLLECT(app_exe, cli_exe, a.binaries, a.datas, name="BioAttend")
