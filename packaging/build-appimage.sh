#!/usr/bin/env bash
# Genera dist/gdsync-<version>-x86_64.AppImage
# Usa Python/GTK4/libadwaita del sistema (Ubuntu 24.04+, Fedora 39+, Debian 13+...).
source "$(dirname "$0")/common.sh"

APPDIR="$ROOT/build/AppDir"
rm -rf "$APPDIR"
stage_app "$APPDIR/usr"

cat > "$APPDIR/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
LIB="$HERE/usr/lib/gdsync"
# «gdsync.AppImage cli <comando>» da acceso al CLI
if [ "${1:-}" = "cli" ]; then shift; exec "$LIB/gdsync" "$@"; fi
if ! python3 -c 'import gi; gi.require_version("Gtk","4.0"); gi.require_version("Adw","1"); from gi.repository import Adw' 2>/dev/null; then
  MSG="gdsync necesita GTK4 y libadwaita. En Ubuntu/Debian: sudo apt install python3-gi gir1.2-adw-1"
  command -v notify-send >/dev/null && notify-send -i dialog-error "Google Drive Sync" "$MSG"
  echo "$MSG" >&2
  exit 1
fi
cd "$LIB" && exec python3 -m gdsync_app "$@"
EOF
chmod 755 "$APPDIR/AppRun"

desktop_entry "AppRun" > "$APPDIR/$APP_ID.desktop"
cp "$ROOT/data/icons/hicolor/scalable/apps/$APP_ID.svg" "$APPDIR/$APP_ID.svg"
ln -sf "$APP_ID.svg" "$APPDIR/.DirIcon"
mkdir -p "$APPDIR/usr/share/metainfo"
cat > "$APPDIR/usr/share/metainfo/$APP_ID.appdata.xml" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<component type="desktop-application">
  <id>$APP_ID</id>
  <metadata_license>MIT</metadata_license>
  <project_license>MIT</project_license>
  <name>Google Drive Sync</name>
  <summary>Keep a Google Drive folder in sync with this computer</summary>
  <description><p>GTK4 app with a tray icon that keeps one Google Drive folder in two-way sync using rclone bisync.</p></description>
  <launchable type="desktop-id">$APP_ID.desktop</launchable>
  <url type="homepage">https://github.com/CamiloFerreira/gdsync</url>
  <releases><release version="$VERSION"/></releases>
</component>
EOF

# appimagetool fijado a una versión y verificado por SHA-256 (cadena de suministro)
AIT_VERSION="1.9.1"
AIT_SHA256="ed4ce84f0d9caff66f50bcca6ff6f35aae54ce8135408b3fa33abfc3cb384eb0"
TOOL="${APPIMAGETOOL:-$ROOT/build/appimagetool-$AIT_VERSION}"
if [[ ! -x "$TOOL" ]]; then
  curl -fsSL --proto '=https' -o "$TOOL.part" \
    "https://github.com/AppImage/appimagetool/releases/download/$AIT_VERSION/appimagetool-x86_64.AppImage"
  echo "$AIT_SHA256  $TOOL.part" | sha256sum -c --quiet - || { rm -f "$TOOL.part"; echo "appimagetool: SHA-256 no coincide" >&2; exit 1; }
  mv "$TOOL.part" "$TOOL"
  chmod +x "$TOOL"
fi
OUT="$DIST/gdsync-${VERSION}-x86_64.AppImage"
ARCH=x86_64 "$TOOL" --appimage-extract-and-run --no-appstream "$APPDIR" "$OUT" >/dev/null 2>&1 \
  || ARCH=x86_64 "$TOOL" --appimage-extract-and-run --no-appstream "$APPDIR" "$OUT"
echo "$OUT"
