#!/usr/bin/env bash
# Genera dist/gdsync_<version>_all.deb
source "$(dirname "$0")/common.sh"

PKG="$ROOT/build/deb"
rm -rf "$PKG"
stage_app "$PKG/usr"

mkdir -p "$PKG/usr/bin" "$PKG/usr/share/applications" \
         "$PKG/usr/share/icons/hicolor/scalable/apps" "$PKG/usr/share/doc/gdsync" "$PKG/DEBIAN"
ln -s ../lib/gdsync/gdsync "$PKG/usr/bin/gdsync"
ln -s ../lib/gdsync/gdsync-desktop "$PKG/usr/bin/gdsync-desktop"
desktop_entry "/usr/bin/gdsync-desktop" > "$PKG/usr/share/applications/$APP_ID.desktop"
chmod 644 "$PKG/usr/share/applications/$APP_ID.desktop"
install -m 644 "$ROOT/data/icons/hicolor/scalable/apps/$APP_ID.svg" "$PKG/usr/share/icons/hicolor/scalable/apps/"
install -m 644 "$ROOT/LICENSE" "$PKG/usr/share/doc/gdsync/copyright"

SIZE="$(du -sk "$PKG/usr" | cut -f1)"
cat > "$PKG/DEBIAN/control" <<EOF
Package: gdsync
Version: $VERSION
Section: net
Priority: optional
Architecture: all
Installed-Size: $SIZE
Depends: python3 (>= 3.10), python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1 (>= 1.5), curl, unzip, systemd
Recommends: gnome-shell-extension-appindicator, libnotify-bin
Maintainer: CamiloFerreira <https://github.com/CamiloFerreira/gdsync>
Homepage: https://github.com/CamiloFerreira/gdsync
Description: Google Drive folder sync for the Linux desktop
 GTK4/libadwaita app with a tray icon that keeps one Google Drive folder
 in two-way sync with a local folder, using rclone bisync and a systemd
 user timer. Includes mandatory security updates.
EOF

find "$PKG" -type d -exec chmod 755 {} +
OUT="$DIST/gdsync_${VERSION}_all.deb"
dpkg-deb --root-owner-group --build "$PKG" "$OUT" >/dev/null
echo "$OUT"
