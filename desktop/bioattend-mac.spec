# PyInstaller spec for BioAttend for macOS (run from the project root by desktop/build_macos.sh):
#   pyinstaller desktop/bioattend-mac.spec --noconfirm
import os

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = SPECPATH + "/.."
VERSION = os.environ.get("BIOATTEND_VERSION", "0.0.0")
APPS = ["config", "core", "employees", "devices", "attendance", "backups"]
LIBS = ["django", "whitenoise", "waitress", "dj_database_url", "dotenv", "openpyxl", "zk", "boto3", "botocore",
        "s3transfer", "paramiko", "cryptography", "requests", "webview", "tzlocal", "tzdata"]


def keep(name):
    if name.rsplit(".", 1)[-1].startswith("test"):  # never ship test modules (tests, test_*.py)
        return False
    return not any(part in name for part in (".tests", ".test.", "django.contrib.gis", "django.db.backends.oracle",
                                             "django.db.backends.mysql", "webview.platforms.android",
                                             "webview.platforms.winforms", "webview.platforms.edgechromium",
                                             "webview.platforms.mshtml", "webview.platforms.cef",
                                             "webview.platforms.gtk", "webview.platforms.qt"))


hidden = ["mac_desktop"]
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


for pkg in ("django", "botocore", "webview", "tzdata"):
    datas += [(src, dest) for src, dest in collect_data_files(pkg) if wanted(src)]

a = Analysis(
    [ROOT + "/desktop/bioattend_desktop.py"],
    pathex=[ROOT, ROOT + "/desktop"],
    binaries=[],  # rclone is downloaded on first use (backups/transports.py: rclone_binary)
    datas=datas,
    hiddenimports=hidden,
    excludes=["tkinter", "gunicorn", "psycopg", "psycopg_binary", "pytest", "IPython", "pystray", "PIL",
              "linux_desktop"],
    noarchive=False,
)
a.datas = [entry for entry in a.datas if wanted(entry[1]) and wanted(entry[0])]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="BioAttend", console=False, argv_emulation=False)
coll = COLLECT(exe, a.binaries, a.datas, name="BioAttend")
app = BUNDLE(
    coll,
    name="BioAttend.app",
    icon=ROOT + "/desktop/bioattend.icns",
    bundle_identifier="com.bioattend.app",
    version=VERSION,
    info_plist={
        "CFBundleName": "BioAttend",
        "CFBundleDisplayName": "BioAttend",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSApplicationCategoryType": "public.app-category.business",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "BioAttend",
        # macOS 15+: needed to reach the biometric devices on the office network
        "NSLocalNetworkUsageDescription": "BioAttend connects to your biometric attendance devices on the local network.",
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
    },
)
