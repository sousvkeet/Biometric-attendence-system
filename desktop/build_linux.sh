#!/usr/bin/env bash
# Builds BioAttend for Linux: AppImage, .deb, .rpm and portable .tar.gz in dist/linux/. Needs Docker.
#   1. almalinux:9 (glibc 2.34, maintained OpenSSL 3): the app itself, a smoke test and the .rpm
#   2. debian:bookworm-slim: .deb, AppImage and .tar.gz
#   desktop/build_linux.sh            version from desktop/bioattend_desktop.py (APP_VERSION)
#   desktop/build_linux.sh 1.2.3
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VERSION="${1:-$(sed -n 's/^APP_VERSION = "\(.*\)"$/\1/p' "$ROOT/desktop/bioattend_desktop.py")}"
OUT="$ROOT/dist/linux"
STAGE="$ROOT/dist/linux-stage"
mkdir -p "$OUT" "$STAGE"
rm -f "$OUT"/*"$VERSION"*
echo "Building BioAttend $VERSION for Linux -> $OUT"
docker run --rm -v "$ROOT":/src:ro -v "$STAGE":/stage -e VERSION="$VERSION" \
    -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" almalinux:9 bash /src/desktop/linux/build_app.sh
docker run --rm -v "$ROOT":/src:ro -v "$STAGE":/stage:ro -v "$OUT":/out -e VERSION="$VERSION" \
    -e HOST_UID="$(id -u)" -e HOST_GID="$(id -g)" debian:bookworm-slim bash /src/desktop/linux/package.sh
rm -rf "$STAGE"
