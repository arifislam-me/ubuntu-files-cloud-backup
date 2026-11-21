#!/usr/bin/python3
"""Ubuntu Files Cloud Backup: GTK4 + libadwaita app. Entry point for the window and the timer."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from engine import (APP_NAME, CLOUD_FOLDER, CONFIG_FILE, LOG_FILE, MAYBE_REAL_WORK, PROVIDERS, scan_skippable, drive_usage, get_account, human_size,
                    install_user_service, load_settings, new_remote_name, pair_log_path, read_progress, save_settings, sync_once,
                    validate_pair, delete_remote)
import oauth


def when(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%d %b %Y, %H:%M")
    except ValueError:
        return iso


def eta_text(seconds) -> str:
    if not seconds:
        return ""
    return f" · {seconds // 3600} h {seconds % 3600 // 60} min left" if seconds >= 3600 else \
        (f" · {seconds // 60} min left" if seconds >= 60 else f" · {seconds} s left")


def tail_file(path, lines: int) -> list[str]:
    try:
        return Path(path).read_text(errors="replace").splitlines()[-lines:]
    except OSError:
        return []


def files_text(n: int) -> str:
    return f"{n:,} file" + ("" if n == 1 else "s")


def duration_text(seconds: int) -> str:
    return f"{seconds} s" if seconds < 60 else f"{seconds // 60} min"


CSS = b"""
.state-run { color: @accent_color; font-weight: bold; }
.state-ok { color: @success_color; font-weight: bold; }
.state-err { color: @error_color; font-weight: bold; }
.state-wait { color: @warning_color; font-weight: bold; }
.folder-error { border: 1px solid @error_color; }
.activity-log { font-family: monospace; font-size: 0.85em; }
"""


def provider_name(account: dict) -> str:
    return PROVIDERS.get(account["provider"], {}).get("name", "Cloud")


def run_gui() -> int:
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Adw", "1")
    from gi.repository import Adw, Gio, GLib, Gtk

    class BackupApp(Adw.Application):
        def __init__(self):
            super().__init__(application_id="me.arifislam.UbuntuFilesCloudBackup")
            self.settings: dict = {"accounts": [], "pairs": []}
            self.busy = False
            self.selected: str | None = None
            self.signature = ""
            self.usage: dict[str, tuple[float, dict | None]] = {}
            self.building = False
            self.live: dict = {}
            self.expanded: set = set()

        # ---------- window ----------
        def do_activate(self):
            if getattr(self, "window", None):
                self.window.present()
                return
            self.window = Adw.ApplicationWindow(application=self, title=APP_NAME)
            self.window.set_default_size(880, 640)
            self.toasts = Adw.ToastOverlay()
            self.root = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
            self.root.add_named(self.build_welcome(), "welcome")
            self.root.add_named(self.build_main(), "main")
            self.toasts.set_child(self.root)
            self.window.set_content(self.toasts)
            breakpoint_ = Adw.Breakpoint.new(Adw.BreakpointCondition.parse("max-width: 640sp"))
            breakpoint_.add_setter(self.split, "collapsed", True)
            self.window.add_breakpoint(breakpoint_)
            for name, handler in (("log", self.on_open_log), ("signout", self.on_sign_out),
                                  ("backup-all", lambda *_: self.run_backup())):
                action = Gio.SimpleAction.new(name, None)
                action.connect("activate", handler)
                self.add_action(action)
            css = Gtk.CssProvider()
            css.load_from_data(CSS)
            Gtk.StyleContext.add_provider_for_display(self.window.get_display(), css,
                                                      Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
            self.window.present()
            subprocess.Popen(["/usr/bin/python3", str(Path(__file__).resolve().parent / "tray.py")],
                             start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.refresh(force=True)
            GLib.timeout_add_seconds(5, self.on_tick)
            GLib.timeout_add_seconds(1, self.on_progress_tick)

        # ---------- welcome (no accounts yet) ----------
        def build_welcome(self):
            view = Adw.ToolbarView()
            header = Adw.HeaderBar()
            header.add_css_class("flat")
            header.set_show_title(False)
            view.add_top_bar(header)
            page = Adw.StatusPage(
                icon_name="weather-overcast-symbolic", title=APP_NAME,
                description="Copy folders from this computer to your cloud storage, automatically. "
                            "Deleting a file here never deletes it in the cloud.")
            clamp = Adw.Clamp(maximum_size=460)
            clamp.set_child(self.provider_group())
            page.set_child(clamp)
            view.set_content(page)
            return view

        def provider_group(self, on_done=None):
            group = Adw.PreferencesGroup(title="Connect a cloud")
            for pid, info in PROVIDERS.items():
                row = Adw.ActionRow(title=info["name"])
                row.add_prefix(Adw.Avatar(size=36, text=info["name"], show_initials=True))
                if info["available"]:
                    row.set_subtitle("Sign in with your browser")
                    button = Gtk.Button(label="Connect", valign=Gtk.Align.CENTER)
                    button.add_css_class("suggested-action")
                    button.connect("clicked", lambda b, p=pid: (on_done and on_done(), self.on_connect(p, button=b)))
                    row.add_suffix(button)
                else:
                    row.set_subtitle("Coming soon")
                    row.set_sensitive(False)
                group.add(row)
            return group

        # ---------- main (sidebar + account page) ----------
        def build_main(self):
            self.split = Adw.NavigationSplitView(min_sidebar_width=250, max_sidebar_width=330)
            self.split.set_sidebar(self.build_sidebar())
            self.split.set_content(self.build_content())
            return self.split

        def build_sidebar(self):
            view = Adw.ToolbarView()
            header = Adw.HeaderBar()
            view.add_top_bar(header)
            self.account_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.SINGLE)
            self.account_list.add_css_class("navigation-sidebar")
            self.account_list.connect("row-selected", self.on_account_selected)
            scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER, vexpand=True)
            scroll.set_child(self.account_list)
            view.set_content(scroll)
            add = Gtk.Button(label="Add cloud account", margin_top=8, margin_bottom=8, margin_start=8, margin_end=8)
            add.add_css_class("pill")
            add.connect("clicked", self.on_add_account)
            view.add_bottom_bar(add)
            self.sidebar_page = Adw.NavigationPage(title="Cloud accounts", child=view)
            return self.sidebar_page

        def build_content(self):
            view = Adw.ToolbarView()
            header = Adw.HeaderBar()
            self.title = Adw.WindowTitle(title="", subtitle="")
            header.set_title_widget(self.title)
            menu = Gio.Menu()
            menu.append("Back up everything now", "app.backup-all")
            menu.append("Open sync log", "app.log")
            menu.append("Remove this account", "app.signout")
            header.pack_end(Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, tooltip_text="Menu"))
            view.add_top_bar(header)
            self.banner = Adw.Banner(title="Login expired", button_label="Sign in again")
            self.banner.connect("button-clicked", lambda *_: self.on_connect(self.current_account()["provider"],
                                                                              reauth=self.selected))
            view.add_top_bar(self.banner)

            page = Adw.PreferencesPage()
            storage = Adw.PreferencesGroup(title="Storage")
            self.usage_row = Adw.ActionRow(title="Space", subtitle="Checking…")
            self.usage_bar = Gtk.LevelBar(min_value=0, max_value=1, valign=Gtk.Align.CENTER)
            self.usage_bar.set_size_request(140, -1)
            self.usage_row.add_suffix(self.usage_bar)
            storage.add(self.usage_row)
            page.add(storage)

            self.folder_group = Adw.PreferencesGroup(title="Folders")
            add_folder = Gtk.Button(label="Add folder", valign=Gtk.Align.CENTER)
            add_folder.add_css_class("suggested-action")
            add_folder.connect("clicked", self.on_add_folder)
            self.folder_group.set_header_suffix(add_folder)
            self.empty_row = Adw.ActionRow(title="No folders yet",
                                           subtitle="Press “Add folder” to choose a folder to back up")
            self.folder_group.add(self.empty_row)
            page.add(self.folder_group)
            self.rows: list = []
            view.set_content(page)
            self.content_page = Adw.NavigationPage(title="Account", child=view)
            return self.content_page

        # ---------- helpers ----------
        def toast(self, text: str):
            self.toasts.add_toast(Adw.Toast(title=text))

        def run_async(self, function, callback):
            def worker():
                try:
                    value = function()
                except Exception as exc:  # shown to the user, never swallowed
                    value = exc
                GLib.idle_add(callback, value)
            threading.Thread(target=worker, daemon=True).start()

        def alert(self, heading: str, body: str, responses=(("ok", "OK"),), suggested=None, callback=None):
            dialog = Adw.AlertDialog(heading=heading, body=body)
            for rid, label in responses:
                dialog.add_response(rid, label)
            if suggested:
                dialog.set_response_appearance(suggested, Adw.ResponseAppearance.SUGGESTED)
            dialog.set_close_response(responses[0][0])
            if callback:
                dialog.connect("response", lambda _d, rid: callback(rid))
            dialog.present(self.window)

        def lookup(self, pair_id):
            return next((p for p in self.settings["pairs"] if p["id"] == pair_id), None)

        def current_account(self):
            return get_account(self.settings, self.selected)

        # ---------- state ----------
        def on_tick(self):
            if not self.busy:
                self.refresh()
            return True

        def refresh(self, force: bool = False):
            self.settings = load_settings()
            signature = json.dumps(self.settings, sort_keys=True)
            if signature == self.signature and not force:
                return
            self.signature = signature
            if self.settings.get("settings_error"):
                self.alert("Settings could not be read", f"The config file may be damaged: {CONFIG_FILE}")
            accounts = self.settings["accounts"]
            self.root.set_visible_child_name("main" if accounts else "welcome")
            if not accounts:
                return
            if not get_account(self.settings, self.selected):
                self.selected = accounts[0]["id"]
            self.build_sidebar_rows()
            self.show_account()

        def build_sidebar_rows(self):
            self.building = True
            while (child := self.account_list.get_first_child()) is not None:
                self.account_list.remove(child)
            for account in self.settings["accounts"]:
                row = Adw.ActionRow(title=provider_name(account), subtitle=account.get("email") or "Connected")
                row.add_prefix(Adw.Avatar(size=36, text=provider_name(account), show_initials=True))
                if account.get("needs_signin"):
                    warn = Gtk.Image(icon_name="dialog-warning-symbolic", tooltip_text="Sign in again")
                    row.add_suffix(warn)
                row.account_id = account["id"]
                row.set_activatable(True)
                self.account_list.append(row)
                if account["id"] == self.selected:
                    self.account_list.select_row(row)
            self.building = False

        def on_account_selected(self, _list, row):
            if self.building or row is None or row.account_id == self.selected:
                if row is not None and not self.building:
                    self.split.set_show_content(True)
                return
            self.selected = row.account_id
            self.show_account()
            self.split.set_show_content(True)

        def show_account(self):
            account = self.current_account()
            if not account:
                return
            self.title.set_title(provider_name(account))
            self.title.set_subtitle(account.get("email") or "")
            self.content_page.set_title(provider_name(account))
            self.banner.set_title(f"{provider_name(account)} login expired")
            self.banner.set_revealed(bool(account.get("needs_signin")))
            self.folder_group.set_description(
                f"Copied to {provider_name(account)} › {CLOUD_FOLDER}. Checked every five minutes.")
            self.update_usage()
            for row in self.rows:
                self.folder_group.remove(row)
            self.rows = []
            self.live = {}
            pairs = [p for p in self.settings["pairs"] if p.get("account") == account["id"]]
            self.empty_row.set_visible(not pairs)
            for pair in pairs:
                row = self.build_pair_row(pair)
                self.folder_group.add(row)
                self.rows.append(row)

        def update_usage(self, force: bool = False):
            account = self.current_account()
            if not account:
                return
            stamp, data = self.usage.get(account["id"], (0.0, None))
            if stamp:
                self.show_usage(account["id"], data)
            if not force and stamp and time.monotonic() - stamp < 600:
                return
            self.usage[account["id"]] = (time.monotonic(), data)
            remote = account["remote"]
            self.run_async(lambda: drive_usage(remote), lambda res, aid=account["id"]: self.store_usage(aid, res))

        def store_usage(self, account_id, usage):
            data = usage if isinstance(usage, dict) else None
            self.usage[account_id] = (time.monotonic(), data)
            if account_id == self.selected:
                self.show_usage(account_id, data)

        def show_usage(self, _account_id, usage):
            if not usage:
                self.usage_row.set_subtitle("Could not read storage")
                self.usage_bar.set_visible(False)
                return
            used, total = usage["used"], usage["total"]
            self.usage_bar.set_visible(True)
            self.usage_bar.set_value(min(1.0, used / total) if total else 0)
            self.usage_row.set_subtitle(
                f"{human_size(used)} used · {human_size(usage['free'])} free of {human_size(total)}")

        def pair_summary(self, pair):
            """Static (not running) tag, state class and two status lines for a folder."""
            status = str(pair.get("last_status") or "")
            if status.startswith(("Error", "Local folder")) or "login expired" in status:
                return "Needs attention", "state-err", status[:300]
            if not pair.get("last_sync"):
                return "Waiting", "state-wait", status or "Waiting for first backup"
            first = "Last backup " + when(pair["last_sync"])
            run = pair.get("last_run") or {}
            if run.get("seconds") is not None:
                first += f" · took {duration_text(run['seconds'])}"
            second = []
            if pair.get("bytes") is not None:
                second.append(f"{pair.get('files', 0)} files · {human_size(pair['bytes'])} in the cloud")
            if run:
                second.append(f"last run: {run.get('copied', 0)} new or changed files")
            return "Up to date", "state-ok", first + ("\n" + " · ".join(second) if second else "")

        def build_pair_row(self, pair):
            pid = pair["id"]
            tag_text, state, status_text = self.pair_summary(pair)
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, css_classes=["card"], margin_bottom=10)
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=12, margin_bottom=12,
                            margin_start=14, margin_end=14)
            card.append(inner)
            top = Gtk.Box(spacing=12)
            top.append(Gtk.Image(icon_name="folder-symbolic", pixel_size=20))
            names = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, hexpand=True, valign=Gtk.Align.CENTER)
            title = Gtk.Label(label=Path(pair["local_path"]).name or pair["local_path"], xalign=0,
                              ellipsize=3, css_classes=["heading"])
            path = Gtk.Label(label=str(pair["local_path"]).replace(str(Path.home()), "~"), xalign=0,
                             ellipsize=3, css_classes=["dim-label", "caption"])
            names.append(title)
            names.append(path)
            top.append(names)
            tag = Gtk.Label(label=tag_text, valign=Gtk.Align.CENTER)
            top.append(tag)
            skip_btn = Gtk.Button(icon_name="view-list-bullet-symbolic", valign=Gtk.Align.CENTER,
                                  tooltip_text="Choose what to skip", css_classes=["flat"])
            skip_btn.connect("clicked", lambda *_a: self.on_edit_skips(pid))
            run = Gtk.Button(icon_name="view-refresh-symbolic", valign=Gtk.Align.CENTER, tooltip_text="Back up now",
                             css_classes=["flat"])
            run.connect("clicked", lambda *_a: self.on_run_now(pid))
            remove = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER,
                                tooltip_text="Remove from app", css_classes=["flat"])
            remove.connect("clicked", lambda *_a: self.on_remove(pid))
            switch = Gtk.Switch(valign=Gtk.Align.CENTER, active=bool(pair.get("enabled")),
                                tooltip_text="Automatic backup on or off")
            switch.connect("notify::active", lambda sw, _p: self.on_toggle(pid, sw.get_active()))
            for widget in (skip_btn, run, remove, switch):
                top.append(widget)
            inner.append(top)
            bar = Gtk.ProgressBar(visible=False)
            inner.append(bar)
            status = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"])
            inner.append(status)
            log = Gtk.Label(xalign=0, wrap=False, ellipsize=3, selectable=True, visible=False,
                            css_classes=["activity-log", "dim-label"])
            inner.append(log)
            more = Gtk.Button(halign=Gtk.Align.START, css_classes=["flat", "caption"], visible=False)
            more.connect("clicked", lambda *_a: self.toggle_expand(pid))
            inner.append(more)
            self.live[pid] = {"card": card, "tag": tag, "bar": bar, "status": status, "log": log, "more": more,
                              "static": (tag_text, state, status_text), "shown": None}
            self.paint_pair(pid, None)
            return card

        def toggle_expand(self, pid):
            self.expanded.symmetric_difference_update({pid})
            self.paint_pair(pid, read_progress() if read_progress() and read_progress().get("pair") == pid else None)

        def paint_pair(self, pid, progress):
            w = self.live.get(pid)
            if not w:
                return
            expanded = pid in self.expanded
            if progress:
                tag_text, state = "Backing up", "state-run"
                if progress.get("scanning") or not progress.get("files"):
                    status_text, fraction = "Checking what needs to be uploaded…", None
                else:
                    status_text = (f"{progress['done']} of {progress['files']} files · "
                                   f"{human_size(progress['bytes'])} of {human_size(progress['total'])}  ·  "
                                   f"{human_size(progress['speed'])}/s{eta_text(progress.get('eta'))}")
                    fraction = progress["bytes"] / progress["total"] if progress["total"] else None
                w["bar"].set_visible(True)
                if fraction is None:
                    w["bar"].pulse()
                else:
                    w["bar"].set_fraction(min(1.0, fraction))
            else:
                tag_text, state, status_text = w["static"]
                w["bar"].set_visible(False)
            for cls in ("state-run", "state-ok", "state-err", "state-wait"):
                w["tag"].remove_css_class(cls)
            w["tag"].add_css_class(state)
            w["tag"].set_label(tag_text)
            if state == "state-err":
                w["card"].add_css_class("folder-error")
                w["status"].add_css_class("error")
            else:
                w["card"].remove_css_class("folder-error")
                w["status"].remove_css_class("error")
            w["status"].set_label(status_text)
            lines = tail_file(pair_log_path(pid), 25 if expanded else 3)
            show_log = bool(lines) and (expanded or progress is not None or state == "state-err")
            w["log"].set_visible(show_log)
            if show_log:
                w["log"].set_label("\n".join(lines))
            w["more"].set_visible(bool(lines))
            w["more"].set_label("Show less" if expanded else ("Show all activity" if show_log else "Show activity"))

        def on_progress_tick(self):
            progress = read_progress()
            for pid in list(self.live):
                self.paint_pair(pid, progress if progress and progress.get("pair") == pid else None)
            return True

        # ---------- accounts ----------
        def on_add_account(self, *_):
            dialog = Adw.Dialog(title="Add cloud account", content_width=420)
            view = Adw.ToolbarView()
            view.add_top_bar(Adw.HeaderBar())
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, margin_top=12, margin_bottom=24,
                          margin_start=16, margin_end=16)
            box.append(self.provider_group(on_done=dialog.close))
            view.set_content(box)
            dialog.set_child(view)
            dialog.present(self.window)

        def on_connect(self, provider: str, button=None, reauth: str | None = None):
            if self.busy:
                return
            self.busy = True
            self.pending_button = button
            if button:
                button.set_sensitive(False)
                button.set_label("Waiting…")
            self.banner.set_revealed(False)
            self.toast("Continue in your browser…")
            existing = get_account(self.settings, reauth)
            remote = new_remote_name(provider)
            self.run_async(lambda: oauth.sign_in(remote),
                           lambda res: self.finish_connect(res, provider, remote, existing))

        def finish_connect(self, result, provider, remote, existing):
            self.busy = False
            button = getattr(self, "pending_button", None)
            if button:
                button.set_sensitive(True)
                button.set_label("Connect")
            if isinstance(result, Exception):
                self.alert("Sign-in failed", str(result))
                return
            email = result
            settings = load_settings()
            target = get_account(settings, existing["id"]) if existing else None
            if not target and email:
                target = next((a for a in settings["accounts"]
                               if a["provider"] == provider and a.get("email") == email), None)
            if target:
                old = target["remote"]
                target.update(remote=remote, email=email or target.get("email", ""))
                target.pop("needs_signin", None)
                if old != remote:
                    delete_remote(old)
            else:
                target = {"id": remote, "provider": provider, "remote": remote, "email": email}
                settings["accounts"].append(target)
            self.selected = target["id"]
            save_settings(settings)
            self.refresh(force=True)
            self.toast(f"Connected {PROVIDERS[provider]['name']}" + (f" as {email}" if email else ""))
            self.window.present()

        def on_sign_out(self, *_):
            account = self.current_account()
            if not account:
                return
            self.alert(f"Remove {provider_name(account)}?",
                       "Automatic backups for this account stop. Files already in the cloud stay where they are.",
                       (("cancel", "Cancel"), ("out", "Remove account")), suggested=None,
                       callback=lambda rid: rid == "out" and self.do_sign_out(account["id"]))

        def do_sign_out(self, account_id):
            settings = load_settings()
            account = get_account(settings, account_id)
            if not account:
                return
            oauth.sign_out(account["remote"])
            settings["accounts"] = [a for a in settings["accounts"] if a["id"] != account_id]
            settings["pairs"] = [p for p in settings["pairs"] if p.get("account") != account_id]
            save_settings(settings)
            self.selected = None
            self.refresh(force=True)

        def on_open_log(self, *_):
            if LOG_FILE.exists():
                Gio.AppInfo.launch_default_for_uri(LOG_FILE.as_uri(), None)
            else:
                self.toast("No sync log yet")

        # ---------- folders ----------
        def on_add_folder(self, *_):
            chooser = Gtk.FileDialog(title="Choose a folder to back up")
            chooser.select_folder(self.window, None, self.folder_chosen)

        def folder_chosen(self, chooser, result):
            try:
                folder = chooser.select_folder_finish(result)
            except GLib.Error:
                return
            account = self.current_account()
            local = folder.get_path()
            if not local or not account:
                self.alert("Unsupported folder", "Choose a folder on this computer.")
                return
            mine = [p for p in self.settings["pairs"] if p.get("account") == account["id"]]
            taken = {p["remote_path"] for p in mine}
            name = Path(local).name or "Backup"
            destination, n = name, 2
            while destination in taken:
                destination, n = f"{name}-{n}", n + 1
            error = validate_pair(local, destination, mine)
            if error:
                self.alert("Cannot add this folder", error)
                return
            self.busy = True
            self.toast("Looking for things you may not want to upload…")
            self.run_async(lambda: scan_skippable(local),
                           lambda res: self.after_scan(res, account["id"], local, destination))

        def after_scan(self, scan, account_id, local, destination):
            self.busy = False
            if isinstance(scan, Exception) or (not scan["groups"] and not scan["ignored"] and not scan["secrets"]):
                self.add_pair(account_id, local, destination, {})
                return
            self.open_skip_dialog(scan, None, "Add and start backup",
                                  lambda skip: self.add_pair(account_id, local, destination, skip))

        def add_pair(self, account_id, local, destination, skip):
            pair = {"id": str(uuid.uuid4()), "account": account_id, "local_path": str(Path(local).resolve()),
                    "remote_path": destination, "enabled": True, "approved": True, "skip": skip,
                    "last_status": "Starting first backup", "last_sync": None}
            self.settings["pairs"].append(pair)
            save_settings(self.settings)
            ok, details = install_user_service()
            if not ok:
                self.alert("Could not set up the background timer", details[-1500:])
            self.refresh(force=True)
            self.run_backup()

        def on_edit_skips(self, pid):
            pair = self.lookup(pid)
            if not pair or self.busy:
                return
            self.busy = True
            self.toast("Scanning the folder…")
            self.run_async(lambda: scan_skippable(pair["local_path"]), lambda res: self.after_edit_scan(res, pid))

        def after_edit_scan(self, scan, pid):
            self.busy = False
            pair = self.lookup(pid)
            if not pair or isinstance(scan, Exception):
                self.alert("Scan failed", str(scan))
                return
            if not scan["groups"] and not scan["ignored"] and not scan["secrets"]:
                self.toast("Nothing to skip was found in this folder")
                return

            def save(skip):
                pair["skip"] = skip
                save_settings(self.settings)
                self.refresh(force=True)
                self.toast("Saved. It applies from the next backup. Items already in the cloud stay there.")
            self.open_skip_dialog(scan, pair.get("skip"), "Save", save)

        def open_skip_dialog(self, scan, initial, confirm_label, on_confirm):
            dialog = Adw.Dialog(title="Choose what to skip", content_width=600, content_height=680)
            view = Adw.ToolbarView()
            view.add_top_bar(Adw.HeaderBar())
            page = Adw.PreferencesPage()
            guard = [False]
            checks: dict = {}
            git_check = None
            total = Gtk.Label(xalign=0, wrap=True, css_classes=["dim-label"], hexpand=True)

            def update_total(*_):
                size = files = 0
                for _name, (_gc, locs) in checks.items():
                    for chk, loc in locs:
                        if chk.get_active():
                            size, files = size + loc["bytes"], files + loc["files"]
                text = f"Will skip {human_size(size)} · {files_text(files)}"
                if git_check is not None and git_check.get_active():
                    text += ", plus everything in .gitignore"
                total.set_label(text)

            if scan["ignored"]:
                group = Adw.PreferencesGroup()
                row = Adw.ActionRow(
                    title="Skip everything in .gitignore",
                    subtitle=f"git lists {scan['ignored']:,} ignored items in {scan['repos']} project(s). "
                             "Files such as .env are usually ignored too, so they would not be backed up.")
                row.set_subtitle_lines(3)
                git_check = Gtk.CheckButton(valign=Gtk.Align.CENTER,
                                            active=bool(initial.get("git")) if initial is not None else True)
                git_check.connect("toggled", update_total)
                row.add_prefix(git_check)
                row.set_activatable_widget(git_check)
                group.add(row)
                page.add(group)

            entries = []
            if scan["secrets"]:
                entries.append(("__secrets__", scan["secrets"]))
            entries += [(n, scan["groups"][n]) for n in sorted(
                scan["groups"], key=lambda n: -sum(loc["bytes"] for loc in scan["groups"][n]))]
            if entries:
                group = Adw.PreferencesGroup(title="Found in this folder",
                                             description="Ticked items are not uploaded. Matching folders created later are skipped too.")
                for name, locs_data in entries:
                    is_secret = name == "__secrets__"
                    if is_secret:
                        expander = Adw.ExpanderRow(
                            title="Secrets and keys",
                            subtitle=f"{files_text(len(locs_data))} that usually hold passwords or tokens (.env, token.json, keys). Matching files added later are skipped too.")
                        expander.set_subtitle_lines(3)
                    else:
                        expander = Adw.ExpanderRow(
                            title=name, subtitle=f"{len(locs_data)} place" + ("" if len(locs_data) == 1 else "s") + f" · {files_text(sum(l['files'] for l in locs_data))}")
                    expander.add_suffix(Gtk.Label(label=human_size(sum(l["bytes"] for l in locs_data)),
                                                  css_classes=["dim-label"]))
                    default_on = is_secret or name not in MAYBE_REAL_WORK
                    group_check = Gtk.CheckButton(valign=Gtk.Align.CENTER)
                    expander.add_prefix(group_check)
                    loc_checks = []
                    for loc in locs_data:
                        if initial is None:
                            active = default_on
                        elif is_secret:
                            active = bool(initial.get("secrets")) or loc["path"] in initial.get("secret_paths", [])
                        else:
                            active = name in initial.get("names", []) or loc["path"] in initial.get("paths", [])
                        sub = Adw.ActionRow(title=loc["path"], subtitle=f"{files_text(loc['files'])} · {human_size(loc['bytes'])}")
                        sub.set_title_lines(1)
                        chk = Gtk.CheckButton(valign=Gtk.Align.CENTER, active=active)
                        sub.add_prefix(chk)
                        sub.set_activatable_widget(chk)
                        expander.add_row(sub)
                        loc_checks.append((chk, loc))
                    checks[name] = (group_check, loc_checks)

                    def sync_group(*_a, gc=group_check, lc=loc_checks):
                        if guard[0]:
                            return
                        guard[0] = True
                        states = [c.get_active() for c, _l in lc]
                        gc.set_inconsistent(any(states) and not all(states))
                        gc.set_active(all(states))
                        guard[0] = False
                        update_total()

                    def toggle_all(gc, lc=loc_checks):
                        if guard[0]:
                            return
                        guard[0] = True
                        gc.set_inconsistent(False)
                        for c, _l in lc:
                            c.set_active(gc.get_active())
                        guard[0] = False
                        update_total()

                    for chk, _loc in loc_checks:
                        chk.connect("toggled", sync_group)
                    group_check.connect("toggled", toggle_all)
                    sync_group()
                    group.add(expander)
                page.add(group)
            update_total()
            view.set_content(page)

            def confirm(*_):
                skip = {"names": [], "paths": [], "git": bool(git_check is not None and git_check.get_active())}
                for name, (_gc, locs) in checks.items():
                    chosen = [loc["path"] for chk, loc in locs if chk.get_active()]
                    if name == "__secrets__":
                        if len(chosen) == len(locs):
                            skip["secrets"] = True
                        else:
                            skip["secret_paths"] = chosen
                    elif len(chosen) == len(locs):
                        skip["names"].append(name)
                    else:
                        skip["paths"].extend(chosen)
                dialog.close()
                on_confirm(skip)

            bar = Gtk.Box(spacing=8, margin_top=10, margin_bottom=10, margin_start=14, margin_end=14)
            bar.append(total)
            cancel = Gtk.Button(label="Cancel")
            cancel.connect("clicked", lambda *_: dialog.close())
            ok = Gtk.Button(label=confirm_label, css_classes=["suggested-action"])
            ok.connect("clicked", confirm)
            bar.append(cancel)
            bar.append(ok)
            view.add_bottom_bar(bar)
            dialog.set_child(view)
            dialog.present(self.window)

        def run_backup(self):
            self.busy = True
            self.toast("Backing up…")
            self.run_async(sync_once, self.finish_backup)

        def finish_backup(self, result):
            self.busy = False
            self.update_usage(force=True)
            self.refresh(force=True)
            failed = isinstance(result, Exception) or result != 0
            self.toast("Backup needs attention, see the folder status" if failed else "Backup finished")

        def on_run_now(self, pair_id):
            pair = self.lookup(pair_id)
            if pair:
                self.run_backup()

        def on_toggle(self, pair_id, active):
            pair = self.lookup(pair_id)
            if pair and bool(pair.get("enabled")) != active:
                pair["enabled"] = active
                save_settings(self.settings)
                self.signature = json.dumps(self.settings, sort_keys=True)

        def on_remove(self, pair_id):
            self.alert("Remove this folder?", "It stops being backed up. Files already in the cloud are kept.",
                       (("cancel", "Cancel"), ("remove", "Remove")),
                       callback=lambda rid: rid == "remove" and self.do_remove(pair_id))

        def do_remove(self, pair_id):
            self.settings["pairs"] = [p for p in self.settings["pairs"] if p["id"] != pair_id]
            save_settings(self.settings)
            self.refresh(force=True)

    return BackupApp().run(None)


def main() -> int:
    os.umask(0o077)
    if "--sync-once" in sys.argv:
        return sync_once()
    if "--install-service" in sys.argv:
        ok, output = install_user_service()
        print(output)
        return 0 if ok else 1
    return run_gui()


if __name__ == "__main__":
    raise SystemExit(main())
