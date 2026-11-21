#!/usr/bin/env bash
# Build ubuntu-files-cloud-backup_<version>_all.deb from this checkout.
# Usage: packaging/build-deb.sh 1.0.0
set -euo pipefail
VERSION="${1:?usage: build-deb.sh VERSION}"
VERSION="${VERSION#v}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ID="me.arifislam.UbuntuFilesCloudBackup"
PKG="ubuntu-files-cloud-backup"
LIB="usr/lib/$PKG"
OUT="$ROOT/dist"
STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

install -d -m 755 "$STAGE/$LIB" "$STAGE/usr/bin" "$STAGE/usr/share/applications" \
  "$STAGE/usr/share/icons/hicolor/scalable/apps" "$STAGE/etc/xdg/autostart" "$STAGE/usr/share/doc/$PKG" "$STAGE/DEBIAN"

for f in engine.py oauth.py gdrive_sync.py tray.py icon.svg; do
  install -m 644 "$ROOT/$f" "$STAGE/$LIB/$f"
done
chmod 755 "$STAGE/$LIB/gdrive_sync.py" "$STAGE/$LIB/tray.py"
install -m 644 "$ROOT/icon.svg" "$STAGE/usr/share/icons/hicolor/scalable/apps/$ID.svg"
install -m 644 "$ROOT/LICENSE" "$STAGE/usr/share/doc/$PKG/copyright"
install -m 644 "$ROOT/README.md" "$ROOT/oauth-setup.md" "$ROOT/PRIVACY.md" "$STAGE/usr/share/doc/$PKG/"

printf '#!/bin/sh\nexec /usr/bin/python3 /%s/gdrive_sync.py "$@"\n' "$LIB" > "$STAGE/usr/bin/$PKG"
printf '#!/bin/sh\nexec /usr/bin/python3 /%s/tray.py "$@"\n' "$LIB" > "$STAGE/usr/bin/$PKG-tray"
chmod 755 "$STAGE/usr/bin/$PKG" "$STAGE/usr/bin/$PKG-tray"

cat > "$STAGE/usr/share/applications/$ID.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=Ubuntu Files Cloud Backup
Comment=Back up selected folders to Google Drive
Exec=/usr/bin/$PKG
Icon=$ID
Terminal=false
Categories=Utility;Network;
StartupWMClass=$ID
DESK
cat > "$STAGE/etc/xdg/autostart/$ID.Tray.desktop" <<TRAY
[Desktop Entry]
Type=Application
Name=Ubuntu Files Cloud Backup (tray icon)
Exec=/usr/bin/$PKG-tray
Icon=$ID
Terminal=false
NoDisplay=true
X-GNOME-Autostart-enabled=true
TRAY
chmod 644 "$STAGE/usr/share/applications/$ID.desktop" "$STAGE/etc/xdg/autostart/$ID.Tray.desktop"

find "$STAGE" -type d -exec chmod 755 {} +
SIZE="$(du -sk "$STAGE" | cut -f1)"
cat > "$STAGE/DEBIAN/control" <<CTRL
Package: $PKG
Version: $VERSION
Section: utils
Priority: optional
Architecture: all
Installed-Size: $SIZE
Depends: python3, python3-gi, gir1.2-gtk-4.0, gir1.2-adw-1, gir1.2-gtk-3.0, gir1.2-ayatanaappindicator3-0.1 | gir1.2-appindicator3-0.1, rclone, git
Recommends: libnotify-bin
Maintainer: arifislam-me <67411663+arifislam-me@users.noreply.github.com>
Homepage: https://github.com/arifislam-me/ubuntu-files-cloud-backup
Description: Back up chosen folders to Google Drive
 A GTK4 and libadwaita app. Connect a Google account, pick folders, and they are
 copied to Google Drive every five minutes by rclone. Deleting a file locally
 never deletes it in the cloud. Includes a top-bar tray icon and a live progress view.
 Google sign-in needs your own OAuth client file, see the bundled oauth-setup.md.
CTRL
chmod 644 "$STAGE/DEBIAN/control"

mkdir -p "$OUT"
dpkg-deb --root-owner-group --build "$STAGE" "$OUT/${PKG}_${VERSION}_all.deb" >/dev/null
( cd "$OUT" && sha256sum "${PKG}_${VERSION}_all.deb" > "${PKG}_${VERSION}_all.deb.sha256" )
echo "Built $OUT/${PKG}_${VERSION}_all.deb"
