#!/usr/bin/env bash
# Funciones compartidas por los scripts de empaquetado.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VERSION="$(cat "$ROOT/VERSION")"
APP_ID="io.github.gdsync"
DIST="$ROOT/dist"
mkdir -p "$DIST"

# Copia la app a <prefix>/lib/gdsync (layout común a .deb y AppImage)
stage_app() {
  local lib="$1/lib/gdsync"
  mkdir -p "$lib"
  cp -r "$ROOT/gdsync_app" "$ROOT/data" "$lib/"
  find "$lib" -name '__pycache__' -prune -exec rm -rf {} +
  find "$lib" -type f -exec chmod 644 {} +
  install -m 755 "$ROOT/gdsync" "$lib/gdsync"
  install -m 755 "$ROOT/gdsync-desktop" "$lib/gdsync-desktop"
  install -m 644 "$ROOT/VERSION" "$lib/VERSION"
}

desktop_entry() {  # $1 = Exec
  cat <<EOF
[Desktop Entry]
Type=Application
Name=Google Drive Sync
GenericName=Google Drive Sync
Comment=Keep a Google Drive folder in sync with this computer
Comment[es]=Mantén una carpeta de Google Drive sincronizada con tu equipo
Exec=$1
Icon=$APP_ID
Terminal=false
StartupNotify=true
Categories=Network;FileTransfer;GTK;
Keywords=google;drive;sync;rclone;cloud;nube;
EOF
}
