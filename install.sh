#!/usr/bin/env bash
set -euo pipefail
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="/usr/bin/python3"
command -v rclone >/dev/null 2>&1 || { echo 'rclone is required: sudo apt install rclone' >&2; exit 1; }
"$PYTHON" -c 'import gi; gi.require_version("Gtk","4.0"); gi.require_version("Adw","1")' 2>/dev/null || {
  echo 'Missing GTK4/libadwaita bindings: sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1' >&2; exit 1; }
ID="me.arifislam.UbuntuFilesCloudBackup"
mkdir -p "$HOME/.local/share/applications" "$HOME/.local/share/icons/hicolor/scalable/apps"
cp "$APP_DIR/icon.svg" "$HOME/.local/share/icons/hicolor/scalable/apps/$ID.svg"
rm -f "$HOME/.local/share/applications/gdrive-folder-backup.desktop"
cat > "$HOME/.local/share/applications/$ID.desktop" <<DESK
[Desktop Entry]
Type=Application
Name=Ubuntu Files Cloud Backup
Comment=Back up selected folders to Google Drive
Exec=$PYTHON $APP_DIR/gdrive_sync.py
Icon=$ID
Terminal=false
Categories=Utility;Network;
StartupWMClass=me.arifislam.UbuntuFilesCloudBackup
DESK
gtk-update-icon-cache -q -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
chmod 644 "$HOME/.local/share/applications/$ID.desktop"
mkdir -p "$HOME/.config/autostart"
cat > "$HOME/.config/autostart/$ID.Tray.desktop" <<TRAY
[Desktop Entry]
Type=Application
Name=Ubuntu Files Cloud Backup (tray icon)
Exec=$PYTHON $APP_DIR/tray.py
Icon=$ID
Terminal=false
NoDisplay=true
X-GNOME-Autostart-enabled=true
TRAY
"$PYTHON" -c 'import gi
gi.require_version("Gtk","3.0")
try:
    gi.require_version("AyatanaAppIndicator3","0.1")
except ValueError:
    gi.require_version("AppIndicator3","0.1")' 2>/dev/null || echo 'Tray icon needs: sudo apt install gir1.2-ayatanaappindicator3-0.1 (or gir1.2-appindicator3-0.1)' >&2
(setsid "$PYTHON" "$APP_DIR/tray.py" >/dev/null 2>&1 &) || true
echo 'Installed. Open "Ubuntu Files Cloud Backup" from Activities.'
[ -f "$HOME/.config/gdrive-folder-backup/oauth-client.json" ] || echo 'Next: create a Google OAuth client, see oauth-setup.md'
