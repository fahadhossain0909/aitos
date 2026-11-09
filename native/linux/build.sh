#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
PYTHON_BIN="${PYTHON_BIN:-python3}"
NODE_BIN="${NODE_BIN:-node}"
VERSION="${AITOS_VERSION:-0.1.0}"
ARCH="$(dpkg --print-architecture)"
case "$ARCH" in amd64|arm64) ;; *) echo "Unsupported Debian architecture: $ARCH" >&2; exit 2 ;; esac
"$NODE_BIN" --version
"$PYTHON_BIN" --version
command -v dpkg-deb >/dev/null
rm -rf .venv-linux-package build/linux dist/linux artifacts/linux
"$PYTHON_BIN" -m venv .venv-linux-package
.venv-linux-package/bin/python -m pip install --upgrade pip
.venv-linux-package/bin/python -m pip install -r native/linux/requirements.txt
npm --prefix desktop ci
npm --prefix desktop run build

STAGE="$ROOT/build/linux/deb-root"
rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN" "$STAGE/opt/aitos/app/native/linux" \
  "$STAGE/usr/bin" "$STAGE/usr/share/applications" \
  "$STAGE/usr/share/icons/hicolor/512x512/apps"
cp -a "$ROOT/.venv-linux-package" "$STAGE/opt/aitos/.venv"
cp -a "$ROOT/aitos" "$STAGE/opt/aitos/app/"
cp "$ROOT/native/linux/launcher.py" "$STAGE/opt/aitos/app/native/linux/launcher.py"
cp -a "$ROOT/desktop/dist" "$STAGE/opt/aitos/app/console_dist"
cat > "$STAGE/usr/bin/aitos-console" <<'WRAPPER'
#!/bin/sh
export PYTHONPATH=/opt/aitos/app
exec /opt/aitos/.venv/bin/python /opt/aitos/app/native/linux/launcher.py "$@"
WRAPPER
chmod 0755 "$STAGE/usr/bin/aitos-console"
cp desktop/public/icon-512.png "$STAGE/usr/share/icons/hicolor/512x512/apps/aitos-console.png"
cat > "$STAGE/usr/share/applications/aitos-console.desktop" <<'DESKTOP'
[Desktop Entry]
Type=Application
Name=AITOS Operator Console
Comment=Authenticated read-only AITOS runtime monitoring
Exec=aitos-console
Icon=aitos-console
Terminal=false
Categories=Office;Finance;
DESKTOP
cat > "$STAGE/DEBIAN/control" <<CONTROL
Package: aitos-console
Version: $VERSION
Section: finance
Priority: optional
Architecture: $ARCH
Maintainer: AITOS Project
Depends: python3, python3-venv, python3-tk, xdg-utils, libglib2.0-0, libsecret-1-0, libdbus-1-3, libx11-6, libxext6, libxrender1, libxss1, libxtst6, libnss3
Description: AITOS authenticated read-only operator console
 Native Linux session manager. Does not install or start trading services.
CONTROL
mkdir -p "$ROOT/artifacts/linux"
dpkg-deb --root-owner-group --build "$STAGE" "$ROOT/artifacts/linux/aitos-console_${VERSION}_${ARCH}.deb"
sha256sum artifacts/linux/*.deb > artifacts/linux/SHA256SUMS
echo "Unsigned Linux test package created under artifacts/linux."
