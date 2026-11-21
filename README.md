<p align="center"><img src="docs/icon.png" width="128" alt="Ubuntu Files Cloud Backup icon"></p>

# Ubuntu Files Cloud Backup

A small GTK4 + libadwaita app for Ubuntu. Connect a cloud account, pick folders, and they are copied to it automatically every five minutes. Google Drive works today; OneDrive and Dropbox are planned (the window already lists them as coming soon). You can connect more than one account. Copying is done by [rclone](https://rclone.org/).

## How it works

1. Press **Sign in with Google**. Your browser opens, you approve, and the app finishes the login by itself. No terminal, no rclone wizard.
2. Press **+** and choose a folder. The app shows what it would upload. Press **Start backup** to confirm.
3. A systemd user timer keeps the folder backed up in the background.

## Install

```bash
sudo apt install rclone python3-gi gir1.2-gtk-4.0 gir1.2-adw-1
./install.sh
```

Then open **Ubuntu Files Cloud Backup** from Activities.

### One-time Google setup

Google requires every app to have its own OAuth client. Create one once (about 5 minutes) by following [oauth-setup.md](oauth-setup.md) and save the downloaded JSON as `~/.config/gdrive-folder-backup/oauth-client.json`. The client secret stays on your machine and is never part of this repository.

## Safety

- One-way `rclone copy`: deleting a file on your computer never deletes it in Drive.
- Only the limited `drive.file` scope: the app can touch only the files it created, inside `Ubuntu Files Cloud Backup/` in your Drive. It cannot read the rest of your Drive.
- A preview is shown before the first upload, and nothing uploads until you confirm.
- Overlapping folders and mounted cloud folders are rejected to prevent sync loops.
- Settings and the token live in `~/.config/gdrive-folder-backup/` with user-only permissions.
- Sign out in the menu revokes the token at Google.
- If a changed file is uploaded again, Drive may replace its copy. Older versions follow Google's own version history.

## Skipping folders you don't need

When you add a folder, the app looks inside it (nested folders too) for things such as `.venv`, `node_modules`, `__pycache__` and `build`, and for items git ignores. You tick what to skip. Matching folders created later are skipped as well. Change the choice later with the list button on the folder card. Skipping stops new uploads only; items already in the cloud are never deleted. The dialog also lists files that usually hold secrets (`.env`, `token.json`, `credentials.json`, private keys; example files such as `.env.example` are not listed). They are ticked by default, so they are not uploaded unless you untick them. Items skipped through `.gitignore` include git-ignored `.env` files too.

## Background and tray icon

Backups run from a systemd user timer every five minutes, so they continue when the window is closed. `tray.py` adds an icon to the top bar (it starts at login). The icon menu shows the current state, opens the window, and starts a backup now. It needs `gir1.2-ayatanaappindicator3-0.1` or `gir1.2-appindicator3-0.1`, and on GNOME the AppIndicator extension (enabled by default on Ubuntu).

## Files

- `tray.py`: the top-bar icon.
- `gdrive_sync.py`: the window and the timer entry point (`--sync-once`).
- `engine.py`: settings, rclone calls, folder checks.
- `oauth.py`: Google sign-in (loopback redirect with PKCE).
- `tests/`: `python3 -m unittest discover -s tests`

## Limits

Only Google Drive is implemented so far (several Google accounts are fine). One-way backup only. No two-way sync, shared drives or conflict handling. While the Google project is in **Testing**, Google expires the login after seven days; see [oauth-setup.md](oauth-setup.md).

## License

MIT, see `LICENSE`.
