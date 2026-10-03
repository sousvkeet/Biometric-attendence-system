#!/usr/bin/env bash
# Builds BioAttend for macOS - run on a Mac (or the GitHub workflow .github/workflows/mac-app.yml).
# Output in dist/macos/: BioAttend-<version>-macos-<arm64|x86_64>.dmg and .zip, SHA256SUMS, screenshot.
#   desktop/build_macos.sh            version from desktop/bioattend_desktop.py (APP_VERSION)
#   desktop/build_macos.sh 1.2.3
#   PYTHON=python3.13 desktop/build_macos.sh       choose the Python (3.12 or 3.13)
#   BIOATTEND_WINDOW_TEST=1 ...                    also open the window once and take a screenshot
# The app is signed ad hoc (Apple Silicon needs a signature), not with an Apple Developer ID: on first open,
# macOS asks to confirm in System Settings > Privacy & Security > "Open Anyway" (see docs/MAC_APP.md).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
VERSION="${1:-$(sed -n 's/^APP_VERSION = "\(.*\)"$/\1/p' desktop/bioattend_desktop.py)}"
ARCH="$(uname -m)"
PY_BASE="${PYTHON:-python3}"
OUT="$ROOT/dist/macos"
WORK="$ROOT/build/macos-$ARCH"
step() { printf '\n=== %s\n' "$*"; }
mkdir -p "$OUT"
rm -f "$OUT"/*"$VERSION"-macos-"$ARCH"* "$OUT/screenshot-$ARCH.png"

step "Python build environment"
"$PY_BASE" -m venv "$WORK/venv"
PY="$WORK/venv/bin/python"
"$PY" -m pip install -q --disable-pip-version-check -r requirements.txt -r desktop/requirements-desktop-mac.txt
"$PY" -c "import sqlite3, ssl, platform; print('  Python', platform.python_version(), '| SQLite', sqlite3.sqlite_version, '|', ssl.OPENSSL_VERSION)"

step "Icon and static files"
"$PY" desktop/make_icon.py
if [ ! -f desktop/bioattend.icns ]; then  # fallback: Apple's own tools
    ICONSET="$WORK/bioattend.iconset"; mkdir -p "$ICONSET"
    for size in 16 32 128 256 512; do
        sips -z $size $size desktop/bioattend.png --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
        sips -z $((size * 2)) $((size * 2)) desktop/bioattend.png --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
    done
    iconutil -c icns "$ICONSET" -o desktop/bioattend.icns
fi
SECRET_KEY=build-only-not-secret-0123456789abcdefghijklmnopqrstuvwxyz DEBUG=false \
    "$PY" manage.py collectstatic --noinput -v 0

step "PyInstaller"
BIOATTEND_VERSION="$VERSION" "$PY" -m PyInstaller desktop/bioattend-mac.spec --noconfirm --log-level WARN \
    --distpath "$WORK/dist" --workpath "$WORK/work"
APP="$WORK/dist/BioAttend.app"
BIN="$APP/Contents/MacOS/BioAttend"
codesign --force --deep --sign - "$APP"
codesign --verify --deep --strict "$APP"
echo "  $(find "$APP" -type f | wc -l | tr -d ' ') files, $(du -sh "$APP" | cut -f1), $(lipo -archs "$BIN"), signed ad hoc"

step "Smoke test: background service"
SMOKE="$(mktemp -d)"
export HOME="$SMOKE/home" BIOATTEND_HOME="$SMOKE/data"   # never touch this Mac's own BioAttend / LaunchAgents
mkdir -p "$HOME"
"$BIN" manage check --fail-level ERROR >/dev/null
check_portal() {
    "$PY" - "$BIOATTEND_HOME" <<'PYEOF'
import json, sys, urllib.request
port = json.load(open(f"{sys.argv[1]}/run/control.json"))["web_port"]
assert urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=3).status == 200
page = urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10).read().decode()
assert "BioAttend" in page, page[:300]
print(f"  web portal answers on port {port}")
PYEOF
}
"$BIN" --background --now > "$SMOKE/service.log" 2>&1 &
for _ in $(seq 1 60); do sleep 1; check_portal 2>/dev/null && break; done
check_portal || { cat "$SMOKE/service.log"; exit 1; }
test -f "$HOME/Library/LaunchAgents/com.bioattend.app.plist" && echo "  start-at-login LaunchAgent written"

if [ "${BIOATTEND_WINDOW_TEST:-}" = "1" ]; then
    step "Smoke test: the window (opens for 25 s)"
    BIOATTEND_WINDOW_SMOKE_SECONDS=25 "$BIN" > "$SMOKE/window.log" 2>&1 &
    WINDOW_PID=$!
    sleep 18
    screencapture -x "$OUT/screenshot-$ARCH.png" 2>/dev/null && echo "  screenshot saved" || echo "  (no screenshot possible here)"
    wait "$WINDOW_PID" || true
    cat "$SMOKE/window.log"
    grep -q "window loaded: http://127.0.0.1" "$SMOKE/window.log" || { echo "the window did not load the portal"; exit 1; }
fi

"$BIN" --quit
for _ in $(seq 1 20); do sleep 1; pgrep -f "$BIN --background" >/dev/null || break; done
if pgrep -f "$BIN --background" >/dev/null; then echo "the service did not quit"; cat "$SMOKE/service.log"; exit 1; fi
echo "  quit cleanly"
unset BIOATTEND_HOME
rm -rf "$SMOKE"

step "Disk image (.dmg) and .zip"
DMGROOT="$WORK/dmg"
rm -rf "$DMGROOT" && mkdir -p "$DMGROOT"
cp -R "$APP" "$DMGROOT/"
ln -s /Applications "$DMGROOT/Applications"
hdiutil create -quiet -volname "BioAttend $VERSION" -srcfolder "$DMGROOT" -ov -format UDZO \
    "$OUT/BioAttend-$VERSION-macos-$ARCH.dmg"
ditto -c -k --keepParent "$APP" "$OUT/BioAttend-$VERSION-macos-$ARCH.zip"
(cd "$OUT" && shasum -a 256 "BioAttend-$VERSION-macos-$ARCH.dmg" "BioAttend-$VERSION-macos-$ARCH.zip" \
    > "SHA256SUMS-macos-$ARCH-$VERSION.txt")
ls -la "$OUT"
echo "Done"
