#!/usr/bin/python3
"""Top-bar tray icon for Ubuntu Files Cloud Backup.

Runs on its own (GTK 3 is needed for the indicator library, the window uses GTK 4). It shows the backup state,
opens the window, starts a backup now, and keeps running after the window is closed. The real syncing is done by
the systemd user timer, so backups continue whether or not the window or this icon is open.
"""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator
except (ValueError, ImportError):
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3 as AppIndicator
from gi.repository import Gio, GLib, Gtk

PROJECT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_DIR))

from engine import APP_NAME, human_size, load_settings, read_progress  # noqa: E402

TRAY_ID = "me.arifislam.UbuntuFilesCloudBackup.Tray"
GUI = str(PROJECT_DIR / "gdrive_sync.py")


def status_text() -> str:
    settings = load_settings()
    names = {p["id"]: Path(p["local_path"]).name for p in settings["pairs"]}
    progress = read_progress()
    if progress:
        name = names.get(progress.get("pair"), "folder")
        if progress.get("scanning") or not progress.get("total"):
            return f"Checking “{name}”…"
        percent = round(100 * progress["bytes"] / progress["total"]) if progress["total"] else 0
        return f"Backing up “{name}”: {percent}% · {human_size(progress['speed'])}/s"
    if any(str(p.get("last_status", "")).startswith(("Error", "Local folder")) or "login expired" in str(p.get("last_status", ""))
           for p in settings["pairs"]):
        return "Needs attention: open the app"
    stamps = [p["last_sync"] for p in settings["pairs"] if p.get("last_sync")]
    if not stamps:
        return "No backup yet"
    try:
        return "Last backup " + datetime.fromisoformat(max(stamps)).astimezone().strftime("%d %b, %H:%M")
    except ValueError:
        return "Idle"


class Tray(Gtk.Application):
    def __init__(self):
        super().__init__(application_id=TRAY_ID)
        self.indicator = None

    def do_activate(self):
        if self.indicator:
            return
        self.hold()
        self.indicator = AppIndicator.Indicator.new(
            "ubuntu-files-cloud-backup", "icon", AppIndicator.IndicatorCategory.APPLICATION_STATUS)
        self.indicator.set_icon_theme_path(str(PROJECT_DIR))
        self.indicator.set_title(APP_NAME)
        self.indicator.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        menu = Gtk.Menu()
        self.status_item = Gtk.MenuItem(label=status_text())
        self.status_item.set_sensitive(False)
        open_item = Gtk.MenuItem(label="Open Ubuntu Files Cloud Backup")
        open_item.connect("activate", self.open_window)
        now_item = Gtk.MenuItem(label="Back up now")
        now_item.connect("activate", self.back_up_now)
        quit_item = Gtk.MenuItem(label="Quit tray icon")
        quit_item.connect("activate", lambda *_: self.quit())
        for item in (self.status_item, Gtk.SeparatorMenuItem(), open_item, now_item, Gtk.SeparatorMenuItem(), quit_item):
            menu.append(item)
        menu.show_all()
        self.indicator.set_menu(menu)
        self.indicator.set_secondary_activate_target(open_item)
        GLib.timeout_add_seconds(2, self.refresh)

    def refresh(self):
        self.status_item.set_label(status_text())
        return True

    def open_window(self, *_):
        subprocess.Popen(["/usr/bin/python3", GUI], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def back_up_now(self, *_):
        subprocess.Popen(["/usr/bin/python3", GUI, "--sync-once"], start_new_session=True,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    os.umask(0o077)
    raise SystemExit(Tray().run(None))
