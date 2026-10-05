#!/usr/bin/env bash
# Stage 1 of the Linux build (started by desktop/build_linux.sh) - runs in an almalinux:9 container:
# glibc 2.34 and RHEL 9's maintained OpenSSL 3, so the app runs on RHEL/Rocky/Alma 9+, Fedora 35+,
# Ubuntu 22.04+, Debian 12+, Mint 21+.
#   /src    project (read-only)
#   /stage  the built app, icon, launcher, AppImage runtime and the .rpm are written here
#   VERSION e.g. 1.0.5
set -euo pipefail
: "${VERSION:?VERSION is required}"
step() { printf '\n=== %s\n' "$*"; }
# Django needs SQLite 3.37+, RHEL 9 has 3.34: build the current release (checksum published on sqlite.org).
SQLITE_YEAR=2026 SQLITE_ID=3530400
SQLITE_SHA3=454e45f61c6bd75b7420e7190732dea03ce6639c63ada47bbc592f67fc340338
mkdir -p /build

step "System packages (AlmaLinux 9)"
dnf install -y -q python3.12 python3.12-devel binutils gcc make procps-ng findutils rpm-build tar gzip \
    >/dev/null

step "SQLite (sqlite-autoconf-$SQLITE_ID, SHA3-256 checked)"
python3.12 - "$SQLITE_YEAR" "$SQLITE_ID" "$SQLITE_SHA3" <<'PYEOF'
import hashlib, sys, urllib.request
year, sqlite_id, sha3 = sys.argv[1:]
data = urllib.request.urlopen(f"https://www.sqlite.org/{year}/sqlite-autoconf-{sqlite_id}.tar.gz", timeout=120).read()
if hashlib.sha3_256(data).hexdigest() != sha3:
    raise SystemExit("Checksum mismatch for the SQLite source")
open("/build/sqlite.tar.gz", "wb").write(data)
PYEOF
tar -C /build -xzf /build/sqlite.tar.gz
(cd "/build/sqlite-autoconf-$SQLITE_ID" && ./configure --prefix=/build/sqlite --disable-static >/dev/null \
    && make -s -j"$(nproc)" >/dev/null 2>&1 && make -s install >/dev/null)
export LD_LIBRARY_PATH=/build/sqlite/lib  # Python (and PyInstaller, which bundles it) use this SQLite

step "Source"
mkdir -p /build/src
tar -C /src --exclude=.venv --exclude=.buildvenv --exclude=.git --exclude=var --exclude=dist --exclude=build \
    --exclude=staticfiles --exclude='*.pyc' --exclude=__pycache__ --exclude='db.sqlite3*' --exclude=.env \
    --exclude=desktop/vendor --exclude=.playwright-mcp -cf - . | tar -C /build/src -xf -
cd /build/src

step "Python packages"
python3.12 -m venv /build/venv
PY=/build/venv/bin/python
$PY -m pip install -q --disable-pip-version-check -r requirements.txt -r desktop/requirements-desktop-linux.txt
$PY -c "import sqlite3, ssl; v = sqlite3.sqlite_version; print('  SQLite', v, '|', ssl.OPENSSL_VERSION); assert tuple(map(int, v.split('.'))) >= (3, 37)"

step "Icon and static files"
$PY desktop/make_icon.py
SECRET_KEY=build-only-not-secret-0123456789abcdefghijklmnopqrstuvwxyz DEBUG=false \
    $PY manage.py collectstatic --noinput -v 0

step "PyInstaller"
$PY -m PyInstaller desktop/bioattend-linux.spec --noconfirm --log-level WARN \
    --distpath /build/dist --workpath /build/work
APP=/build/dist/bioattend
echo "  $(find "$APP" -type f | wc -l) files, $(du -sh "$APP" | cut -f1)"
# (PyInstaller strips the libraries, so compare the version the bundled SQLite reports, not the bytes)
$PY - "$APP"/_internal/libsqlite3.so.0 <<'PYEOF'
import ctypes, sys
lib = ctypes.CDLL(sys.argv[1])
lib.sqlite3_libversion.restype = ctypes.c_char_p
version = lib.sqlite3_libversion().decode()
assert tuple(map(int, version.split("."))) >= (3, 37), f"bundled SQLite {version} is too old"
print("  bundled SQLite", version)
PYEOF
echo "  needs glibc $(objdump -T "$APP"/bioattend "$APP"/_internal/*.so* 2>/dev/null | grep -o 'GLIBC_[0-9.]*' | sort -Vu | tail -1 | cut -d_ -f2)"

step "Smoke test of the packaged app"
unset LD_LIBRARY_PATH  # the app must run with its own bundled libraries only
export BIOATTEND_HOME=/tmp/smoke
"$APP/bioattend" manage check --fail-level ERROR >/dev/null
"$APP/bioattend" --background > /tmp/smoke.log 2>&1 &
check_portal() {
    $PY - <<'PYEOF'
import json, urllib.request
port = json.load(open("/tmp/smoke/run/control.json"))["web_port"]
assert urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3).status == 200
page = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10).read().decode()
assert "BioAttend" in page, page[:300]
print(f"  web portal answers on port {port}")
PYEOF
}
for _ in $(seq 1 60); do sleep 1; check_portal 2>/dev/null && break; done
check_portal || { cat /tmp/smoke.log; exit 1; }
"$APP/bioattend" --quit
for _ in $(seq 1 20); do sleep 1; pgrep -f "$APP/bioattend --background" >/dev/null || break; done
if pgrep -f "$APP/bioattend --background" >/dev/null; then echo "the app did not quit"; cat /tmp/smoke.log; exit 1; fi
test -f /tmp/smoke/bioattend.sqlite3
echo "  started, served the portal and quit cleanly"
unset BIOATTEND_HOME

step "Launcher and icon"
rm -rf /stage/* && mkdir -p /stage
cp -a "$APP" /stage/bioattend
cp desktop/bioattend.png /stage/bioattend.png
cat > /stage/bioattend.desktop <<'EOF'
[Desktop Entry]
Type=Application
Name=BioAttend
GenericName=Biometric attendance
Comment=Attendance from ZKTeco biometric devices
Exec=bioattend
Icon=bioattend
Terminal=false
Categories=Office;
StartupWMClass=BioAttend
StartupNotify=true
Actions=backup;reset-password;data-folder;quit;

[Desktop Action backup]
Name=Back up now
Exec=bioattend --backup

[Desktop Action reset-password]
Name=Reset administrator password
Exec=bioattend --reset-password

[Desktop Action data-folder]
Name=Open data folder
Exec=bioattend --open-data

[Desktop Action quit]
Name=Quit BioAttend
Exec=bioattend --quit
EOF
# The same quit-before-update / clean-up steps for the .rpm and the .deb
cat > /stage/quit-running.sh <<'EOF'
# Ask running BioAttend apps to quit (keeps their databases consistent) before files are replaced/removed.
for home in /home/* /root; do
    [ -f "$home/.local/share/BioAttend/run/control.json" ] || continue
    owner=$(stat -c %U "$home")
    runuser -u "$owner" -- env HOME="$home" /opt/bioattend/bioattend --quit >/dev/null 2>&1 || true
done
sleep 2
EOF
cat > /stage/remove-autostart.sh <<'EOF'
# Start-at-login entries point at /opt/bioattend, which is gone now (the data folders are kept).
for home in /home/* /root; do rm -f "$home/.config/autostart/bioattend.desktop"; done
EOF

step "AppImage runtime (checked against the SHA-256 digest GitHub publishes)"
$PY - <<'PYEOF'
import hashlib, json, os, urllib.request
request = urllib.request.Request("https://api.github.com/repos/AppImage/type2-runtime/releases/tags/continuous")
if os.environ.get("GITHUB_TOKEN"):  # without a token GitHub allows only 60 API requests per hour
    request.add_header("Authorization", f"Bearer {os.environ['GITHUB_TOKEN']}")
release = json.load(urllib.request.urlopen(request, timeout=30))
asset = next(a for a in release["assets"] if a["name"] == "runtime-x86_64")
expected = (asset.get("digest") or "").removeprefix("sha256:")
if not expected:
    raise SystemExit("GitHub did not publish a checksum for the AppImage runtime - not using it")
data = urllib.request.urlopen(asset["browser_download_url"], timeout=120).read()
if hashlib.sha256(data).hexdigest() != expected:
    raise SystemExit("Checksum mismatch for the AppImage runtime")
open("/stage/runtime-x86_64", "wb").write(data)
print(f"  runtime {asset['updated_at']} sha256 {expected}")
PYEOF

step "Fedora / RHEL package (.rpm)"
mkdir -p /build/rpm/SPECS
cat > /build/rpm/SPECS/bioattend.spec <<EOF
Name:           bioattend
Version:        $VERSION
Release:        1
Summary:        Biometric attendance for ZKTeco devices
License:        Proprietary
URL:            https://github.com/sousvkeet/Biometric-attendence-system
AutoReqProv:    no
Recommends:     xdg-utils, libnotify, zenity
%global debug_package %{nil}
%global __os_install_post %{nil}
# No /usr/lib/.build-id links: the bundled libraries would clash with the same system libraries (e.g. libuuid)
%global _build_id_links none

%description
Web portal, device sync (users, fingerprints, live punches), attendance reports
and backups in one desktop app. Runs in the background and starts at login;
each user's data is kept in ~/.local/share/BioAttend.

%install
mkdir -p %{buildroot}/opt %{buildroot}%{_bindir} %{buildroot}%{_datadir}/applications \
    %{buildroot}%{_datadir}/icons/hicolor/256x256/apps
cp -a /stage/bioattend %{buildroot}/opt/bioattend
ln -s /opt/bioattend/bioattend %{buildroot}%{_bindir}/bioattend
install -m 644 /stage/bioattend.desktop %{buildroot}%{_datadir}/applications/bioattend.desktop
install -m 644 /stage/bioattend.png %{buildroot}%{_datadir}/icons/hicolor/256x256/apps/bioattend.png

%files
/opt/bioattend
%{_bindir}/bioattend
%{_datadir}/applications/bioattend.desktop
%{_datadir}/icons/hicolor/256x256/apps/bioattend.png

%preun
$(cat /stage/quit-running.sh)
exit 0

%postun
if [ "\$1" -eq 0 ]; then
$(cat /stage/remove-autostart.sh)
fi
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q %{_datadir}/applications || true
exit 0

%post
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q %{_datadir}/applications || true
exit 0
EOF
rpmbuild -bb --quiet --define "_topdir /build/rpm" /build/rpm/SPECS/bioattend.spec
cp /build/rpm/RPMS/x86_64/bioattend-"$VERSION"-1.x86_64.rpm /stage/
echo "  $(ls /stage/*.rpm)"
[ -n "${HOST_UID:-}" ] && chown -R "$HOST_UID:${HOST_GID:-$HOST_UID}" /stage
echo "Stage 1 done"
