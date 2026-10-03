#!/usr/bin/env bash
# Stage 2 of the Linux build (started by desktop/build_linux.sh) - runs in a debian:bookworm-slim container
# with Debian's own packaging tools: .deb, AppImage, portable .tar.gz and checksums.
#   /stage  output of stage 1 (read-only)    /out  packages    VERSION e.g. 1.0.5
set -euo pipefail
: "${VERSION:?VERSION is required}"
ARCH=x86_64
DEB_ARCH=amd64
step() { printf '\n=== %s\n' "$*"; }

step "System packages (Debian 12)"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq --no-install-recommends squashfs-tools >/dev/null
mkdir -p /build

step "Portable .tar.gz"
TARDIR=/build/tar/BioAttend-$VERSION
mkdir -p "$TARDIR"
cp -a /stage/bioattend "$TARDIR/bioattend"
cat > "$TARDIR/README.txt" <<EOF
BioAttend $VERSION for Linux (x86_64) - portable

Start:     ./bioattend/bioattend
           The first start adds BioAttend to your applications menu and starts it at login.
Remove:    ./bioattend/bioattend --uninstall   (your data in ~/.local/share/BioAttend is kept)

Guide: https://github.com/sousvkeet/Biometric-attendence-system/blob/main/docs/LINUX_APP.md
EOF
tar -C /build/tar -czf "/out/BioAttend-$VERSION-linux-$ARCH.tar.gz" "BioAttend-$VERSION"

step "Debian / Ubuntu package (.deb)"
DEB=/build/deb
mkdir -p "$DEB/opt" "$DEB/usr/bin" "$DEB/usr/share/applications" "$DEB/usr/share/icons/hicolor/256x256/apps" "$DEB/DEBIAN"
cp -a /stage/bioattend "$DEB/opt/bioattend"
ln -s /opt/bioattend/bioattend "$DEB/usr/bin/bioattend"
install -m 644 /stage/bioattend.desktop "$DEB/usr/share/applications/bioattend.desktop"
install -m 644 /stage/bioattend.png "$DEB/usr/share/icons/hicolor/256x256/apps/bioattend.png"
cat > "$DEB/DEBIAN/control" <<EOF
Package: bioattend
Version: $VERSION
Section: misc
Priority: optional
Architecture: $DEB_ARCH
Installed-Size: $(du -sk "$DEB" | cut -f1)
Maintainer: BioAttend <sgeeks.dev@gmail.com>
Recommends: xdg-utils, libnotify-bin, zenity
Suggests: chromium | google-chrome-stable | microsoft-edge-stable, xclip | wl-clipboard
Homepage: https://github.com/sousvkeet/Biometric-attendence-system
Description: Biometric attendance for ZKTeco devices
 Web portal, device sync (users, fingerprints, live punches), attendance reports
 and backups in one desktop app. Runs in the background and starts at login;
 each user's data is kept in ~/.local/share/BioAttend.
EOF
{ echo '#!/bin/sh'; cat /stage/quit-running.sh; echo 'exit 0'; } > "$DEB/DEBIAN/prerm"
{ echo '#!/bin/sh'; echo 'if [ "$1" = "remove" ] || [ "$1" = "purge" ]; then'; cat /stage/remove-autostart.sh; echo 'fi'
  echo 'command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q /usr/share/applications || true'
  echo 'exit 0'; } > "$DEB/DEBIAN/postrm"
cat > "$DEB/DEBIAN/postinst" <<'EOF'
#!/bin/sh
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database -q /usr/share/applications || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache -q -t /usr/share/icons/hicolor || true
exit 0
EOF
chmod 755 "$DEB/DEBIAN/postinst" "$DEB/DEBIAN/prerm" "$DEB/DEBIAN/postrm"
dpkg-deb --root-owner-group -Zxz --build "$DEB" "/out/bioattend_${VERSION}_${DEB_ARCH}.deb" >/dev/null

step "AppImage"
APPDIR=/build/AppDir
mkdir -p "$APPDIR"
cp -a /stage/bioattend "$APPDIR/bioattend"
cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/bioattend/bioattend" "$@"
EOF
chmod 755 "$APPDIR/AppRun"
cp /stage/bioattend.desktop /stage/bioattend.png "$APPDIR/"
ln -s bioattend.png "$APPDIR/.DirIcon"
mksquashfs "$APPDIR" /build/app.squashfs -root-owned -noappend -comp zstd -quiet
IMAGE="/out/BioAttend-$VERSION-$ARCH.AppImage"
cat /stage/runtime-x86_64 /build/app.squashfs > "$IMAGE"
chmod 755 "$IMAGE"
HOME=/tmp/appimage-test "$IMAGE" --appimage-extract-and-run manage check --fail-level ERROR >/dev/null
echo "  the AppImage starts (checked with --appimage-extract-and-run)"

step "Checksums"
cp /stage/bioattend-"$VERSION"-1.x86_64.rpm /out/
cd /out
sha256sum "BioAttend-$VERSION-$ARCH.AppImage" "bioattend_${VERSION}_${DEB_ARCH}.deb" \
    "bioattend-$VERSION-1.x86_64.rpm" "BioAttend-$VERSION-linux-$ARCH.tar.gz" > "SHA256SUMS-linux-$VERSION.txt"
ls -la /out
[ -n "${HOST_UID:-}" ] && chown -R "$HOST_UID:${HOST_GID:-$HOST_UID}" /out
echo "Done"
