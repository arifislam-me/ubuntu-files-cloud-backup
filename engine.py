"""Sync engine: settings, rclone calls and the background timer. No GUI code here."""
from __future__ import annotations

import fcntl
import fnmatch
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

APP_NAME = "Ubuntu Files Cloud Backup"
APP_DIR = Path.home() / ".config" / "gdrive-folder-backup"
CONFIG_FILE = APP_DIR / "settings.json"
RCLONE_CONFIG = APP_DIR / "rclone.conf"
LOG_FILE = APP_DIR / "sync.log"
LOCK_FILE = APP_DIR / "sync.lock"
PROGRESS_FILE = APP_DIR / "progress.json"
PAIR_LOG_DIR = APP_DIR / "logs"
DEFAULT_REMOTE = "gdrive"
RCLONE = shutil.which("rclone") or (str(Path.home() / ".local/bin/rclone"))
PROJECT_FILE = Path(__file__).resolve().parent / "gdrive_sync.py"
CLOUD_FOLDER = "Ubuntu Files Cloud Backup"

# Cloud services the app knows. "available" means sign-in is implemented; the rest show as "Coming soon".
PROVIDERS = {
    "gdrive": {"name": "Google Drive", "rclone": "drive", "available": True},
    "onedrive": {"name": "Microsoft OneDrive", "rclone": "onedrive", "available": False},
    "dropbox": {"name": "Dropbox", "rclone": "dropbox", "available": False},
}


def ensure_private_dir() -> None:
    APP_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(APP_DIR, 0o700)
    for private in (CONFIG_FILE, LOG_FILE, PROGRESS_FILE, LOCK_FILE, *APP_DIR.glob("excludes-*.txt")):
        try:
            os.chmod(private, 0o600)
        except OSError:
            pass
    if not RCLONE_CONFIG.exists():
        RCLONE_CONFIG.touch(mode=0o600)
    os.chmod(RCLONE_CONFIG, 0o600)


def _migrate(data: dict) -> dict:
    """Upgrade settings written by the single-account version (top-level remote and account_email)."""
    if not isinstance(data.get("accounts"), list):
        data["accounts"] = []
        if data.get("account_email") or data["pairs"]:
            data["accounts"].append({"id": "gdrive", "provider": "gdrive",
                                     "remote": data.get("remote", DEFAULT_REMOTE),
                                     "email": data.get("account_email", "")})
            if data.get("needs_signin"):
                data["accounts"][0]["needs_signin"] = True
    for key in ("remote", "account_email", "needs_signin"):
        data.pop(key, None)
    first = data["accounts"][0]["id"] if data["accounts"] else None
    for pair in data["pairs"]:
        pair.setdefault("account", first)
    return data


def load_settings() -> dict:
    ensure_private_dir()
    if not CONFIG_FILE.exists():
        return {"accounts": [], "pairs": []}
    try:
        data = json.loads(CONFIG_FILE.read_text())
        if not isinstance(data.get("pairs"), list):
            raise ValueError("settings has no folder list")
        return _migrate(data)
    except (OSError, ValueError, json.JSONDecodeError):
        return {"accounts": [], "pairs": [], "settings_error": True}


def get_account(settings: dict, account_id: str | None) -> dict | None:
    return next((a for a in settings["accounts"] if a["id"] == account_id), None)


def new_remote_name(provider: str) -> str:
    return f"{provider}-{uuid.uuid4().hex[:8]}"


def list_remotes() -> set[str]:
    result = run_rclone("listremotes", timeout=20)
    if result.returncode != 0:
        return set()
    return {line.rstrip(":") for line in result.stdout.splitlines() if line.endswith(":")}


def delete_remote(remote: str) -> None:
    run_rclone("config", "delete", remote, timeout=30)


def save_settings(data: dict) -> None:
    ensure_private_dir()
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n")
    os.chmod(tmp, 0o600)
    tmp.replace(CONFIG_FILE)


def rclone_command(*args: str) -> list[str]:
    if not Path(RCLONE).is_file():
        raise RuntimeError("rclone was not found. Install rclone, then reopen this app.")
    return [RCLONE, "--config", str(RCLONE_CONFIG), *args]


def run_rclone(*args: str, timeout: int | None = 3600) -> subprocess.CompletedProcess[str]:
    return subprocess.run(rclone_command(*args), text=True, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, timeout=timeout, check=False)


# Folders that are usually rebuilt from other files and just waste cloud space.
KNOWN_SKIP_DIRS = (".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
                   ".tox", ".next", ".gradle", "build", "dist", "target")
# Not ticked by default: these names can hold real work.
MAYBE_REAL_WORK = ("build", "dist", "target")


# Files that usually hold passwords, tokens or private keys. Example/template files are kept.
SECRET_PATTERNS = (".env", ".env.*", "credentials.json", "token.json", "client_secret*.json", "service-account*.json",
                   "*.pem", "*.key", "*.p12", "*.pfx", "id_rsa", "id_rsa.*", "id_ed25519", "id_ed25519.*",
                   "*.session", "secrets.*")
SECRET_SAFE_NAMES = (".env.example", ".env.sample", ".env.template", ".env.dist")


def is_secret_name(name: str) -> bool:
    lower = name.lower()
    if lower in SECRET_SAFE_NAMES or lower.endswith((".pub", ".example", ".sample")):
        return False
    return any(fnmatch.fnmatch(lower, pattern) for pattern in SECRET_PATTERNS)


def _dir_stats(path: str) -> tuple[int, int]:
    files = size = 0
    for root, _dirs, names in os.walk(path):
        for name in names:
            try:
                info = os.lstat(os.path.join(root, name))
            except OSError:
                continue
            files += 1
            size += info.st_size
    return files, size


def _walk_repos(local: Path, prune: tuple[str, ...] = ()) -> tuple[list[Path], dict, list[dict]]:
    """Walk the folder once: git repositories, known skippable folders (nested too) and secret-looking files."""
    repos: list[Path] = []
    groups: dict[str, list[dict]] = {}
    secrets: list[dict] = []
    for root, dirs, files in os.walk(local):
        if ".git" in dirs or ".git" in files:
            repos.append(Path(root))
        for name in files:
            if is_secret_name(name):
                full = os.path.join(root, name)
                try:
                    size = os.lstat(full).st_size
                except OSError:
                    size = 0
                secrets.append({"path": os.path.relpath(full, local).replace(os.sep, "/"), "files": 1, "bytes": size})
        keep = []
        for name in dirs:
            full = os.path.join(root, name)
            if name == ".git" or os.path.islink(full) or name in prune:
                continue
            if name in KNOWN_SKIP_DIRS:
                count, size = _dir_stats(full)
                rel = os.path.relpath(full, local).replace(os.sep, "/")
                groups.setdefault(name, []).append({"path": rel, "files": count, "bytes": size})
            else:
                keep.append(name)
        dirs[:] = keep
    return repos, groups, secrets


def git_ignored(repo: Path) -> list[str]:
    """Paths git itself ignores in this repository (directories end with a slash)."""
    if not shutil.which("git"):
        return []
    result = subprocess.run(["git", "-C", str(repo), "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
                             "ls-files", "--others", "--ignored", "--exclude-standard",
                             "--directory", "-z"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    if result.returncode != 0:
        return []
    return [item for item in result.stdout.decode(errors="replace").split("\0") if item]


def scan_skippable(local: str) -> dict:
    """Look inside a folder for things to skip. Returns groups (by folder name), git repos and ignored-item count."""
    root = Path(local).expanduser().resolve()
    repos, groups, secrets = _walk_repos(root)
    ignored = sum(len(git_ignored(repo)) for repo in repos)
    return {"groups": groups, "repos": len(repos), "ignored": ignored, "secrets": secrets}


def _escape(text: str) -> str:
    return "".join("\\" + ch if ch in "\\*?[]{}" else ch for ch in text)


def build_excludes(pair: dict, local: Path) -> list[str]:
    """rclone filter lines ("+ " keep, "- " skip) for one folder pair, from its saved skip choices."""
    skip = pair.get("skip") or {}
    rules = []
    if skip.get("secrets"):
        rules += [f"+ {_escape(name)}" for name in SECRET_SAFE_NAMES] + ["+ *.pub"]
        rules += [f"- {pattern}" for pattern in SECRET_PATTERNS]
    rules += [f"- /{_escape(path)}" for path in skip.get("secret_paths", [])]
    rules += [f"- {_escape(name)}/**" for name in skip.get("names", [])]
    rules += [f"- /{_escape(path)}/**" for path in skip.get("paths", [])]
    if skip.get("git"):
        repos, _groups, _secrets = _walk_repos(local, prune=tuple(skip.get("names", [])))
        for repo in repos:
            rel = os.path.relpath(repo, local).replace(os.sep, "/")
            for entry in git_ignored(repo):
                path = entry if rel == "." else f"{rel}/{entry}"
                rules.append(f"- /{_escape(path.rstrip('/'))}/**" if entry.endswith("/") else f"- /{_escape(path)}")
    return rules


def validate_pair(local: str, remote_path: str, pairs: list[dict], skip_id: str | None = None) -> str | None:
    path = Path(local).expanduser().resolve()
    if not path.is_dir():
        return "Choose an existing local folder."
    gvfs = Path(f"/run/user/{os.getuid()}/gvfs").resolve()
    if path == gvfs or gvfs in path.parents:
        return "This looks like a mounted cloud drive. Choose a normal local folder to avoid syncing a cloud with itself."
    try:
        for line in Path("/proc/self/mountinfo").read_text().splitlines():
            fields = line.split()
            if "-" not in fields:
                continue
            separator = fields.index("-")
            fs_type = fields[separator + 1]
            mountpoint = Path(fields[4].replace("\\040", " ")).resolve()
            if fs_type in ("fuse.rclone", "rclone", "fuse.gvfsd-fuse") and (path == mountpoint or mountpoint in path.parents):
                return "This folder is inside a mounted cloud drive. Choose a normal local folder to avoid syncing a cloud with itself."
    except OSError:
        pass
    dest = remote_path.strip().strip("/")
    if not dest:
        return "Enter a destination folder name in the cloud."
    if any(part in (".", "..") for part in dest.split("/")) or ":" in dest:
        return "The cloud destination must be a relative folder path, such as Backups/Documents."
    for item in pairs:
        if item.get("id") == skip_id:
            continue
        other = Path(item.get("local_path", "")).resolve()
        if path == other or path in other.parents or other in path.parents:
            return f"This folder overlaps another sync pair ({other}). Use separate folders."
        if dest == item.get("remote_path") or dest.startswith(item.get("remote_path", "") + "/") or item.get("remote_path", "").startswith(dest + "/"):
            return "Cloud destinations for different pairs must not overlap."
    return None


def remote_exists(remote: str) -> bool:
    return remote in list_remotes()


def pair_remote(settings: dict, pair: dict) -> str:
    account = get_account(settings, pair.get("account"))
    if not account:
        raise RuntimeError("The cloud account for this folder is no longer connected.")
    return f"{account['remote']}:{CLOUD_FOLDER}/{pair['remote_path']}"


def sync_pair(settings: dict, pair: dict, dry_run: bool = False) -> tuple[int, str]:
    local = Path(pair["local_path"]).expanduser().resolve()
    if not local.is_dir():
        return 2, f"Local folder is missing or unavailable: {local}\nNo cloud changes were made."
    try:
        destination = pair_remote(settings, pair)
    except RuntimeError as exc:
        return 2, str(exc)
    args = ["copy", str(local), destination, "--create-empty-src-dirs", "--skip-links"]
    rules = build_excludes(pair, local)
    if rules:
        rules_file = APP_DIR / f"excludes-{pair['id']}.txt"
        rules_file.write_text("\n".join(rules) + "\n")
        args.extend(["--filter-from", str(rules_file)])
    report_path = None
    if dry_run:
        report = tempfile.NamedTemporaryFile(prefix="preview-", suffix=".txt", dir=APP_DIR, delete=False)
        report_path = Path(report.name)
        report.close()
        args.extend(["--dry-run", "--verbose", "--combined", str(report_path)])
    else:
        args.extend(["--use-json-log", "--stats", "1s", "--log-level", "INFO"])
        code, output, summary = _run_streaming(args, pair["id"])
        pair["last_run"] = summary
        return code, output or ("No output." if code == 0 else "rclone failed.")
    result = run_rclone(*args, timeout=None)
    output = result.stdout or ""
    if report_path:
        try:
            lines = report_path.read_text(errors="replace").splitlines()
        except OSError:
            lines = []
        finally:
            report_path.unlink(missing_ok=True)
        changes = [line for line in lines if line.startswith(("+ ", "* "))]
        byte_total = 0
        for line in changes:
            candidate = local / line[2:]
            try:
                if candidate.is_file() and not candidate.is_symlink():
                    byte_total += candidate.stat().st_size
            except OSError:
                pass
        readable = f"{byte_total / (1024 * 1024):.1f} MiB" if byte_total < 1024**3 else f"{byte_total / (1024**3):.2f} GiB"
        summary = f"Planned uploads: {len(changes)} file(s), approximately {readable}.\n"
        summary += "\n".join(lines) if lines else "No file changes listed."
        output = summary + ("\n\n" + output if output.strip() else "")
    return result.returncode, output or ("No output." if result.returncode == 0 else "rclone failed.")


def write_progress(data: dict) -> None:
    tmp = PROGRESS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(PROGRESS_FILE)


def read_progress() -> dict | None:
    """Live state of a running backup, or None when idle (a stale file counts as idle)."""
    try:
        data = json.loads(PROGRESS_FILE.read_text())
    except (OSError, ValueError):
        return None
    if not data.get("running") or time.time() - data.get("updated", 0) > 20:
        return None
    return data


def _trim_log_file(path: Path, limit: int) -> None:
    try:
        if path.stat().st_size > limit:
            tail = path.read_bytes()[-limit // 2:]
            path.write_bytes(tail[tail.find(b"\n") + 1:])
    except OSError:
        pass


def _trim_log(limit: int = 2_000_000) -> None:
    _trim_log_file(LOG_FILE, limit)


def pair_log_path(pair_id: str) -> Path:
    return PAIR_LOG_DIR / f"{pair_id}.log"


def _run_streaming(args: list[str], pair_id: str) -> tuple[int, str, dict]:
    """Run rclone and turn its JSON log into a readable log file plus live progress for the window."""
    _trim_log()
    PAIR_LOG_DIR.mkdir(mode=0o700, exist_ok=True)
    began, copied, last_bytes = time.monotonic(), 0, 0
    started = {"running": True, "pair": pair_id, "updated": time.time(), "scanning": True}
    write_progress(started)
    proc = subprocess.Popen(rclone_command(*args), text=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    errors: list[str] = []
    with LOG_FILE.open("a") as log_all, pair_log_path(pair_id).open("a") as log_pair:
        class _Both:
            def write(self, text: str) -> None:
                log_all.write(text)
                log_pair.write(text)
        log = _Both()
        for raw in proc.stderr:
            line = raw.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                errors.append(line)
                log.write(line + "\n")
                continue
            stats = entry.get("stats")
            if stats:
                last_bytes = stats.get("bytes", 0)
                write_progress({
                    "running": True, "pair": pair_id, "updated": time.time(), "scanning": False,
                    "bytes": stats.get("bytes", 0), "total": stats.get("totalBytes", 0),
                    "done": stats.get("transfers", 0), "files": stats.get("totalTransfers", 0),
                    "speed": stats.get("speed", 0), "eta": stats.get("eta"),
                    "current": [t.get("name", "") for t in stats.get("transferring", [])[:2]]})
                continue
            level, message = entry.get("level", ""), entry.get("msg", "")
            name = entry.get("object")
            stamp = str(entry.get("time", ""))[:19].replace("T", " ")
            log.write(f"{stamp} {level.upper():5} {name + ': ' if name else ''}{message}\n")
            if message.startswith("Copied"):
                copied += 1
            if level in ("error", "critical"):
                errors.append(message)
    code = proc.wait()
    write_progress({"running": False, "pair": pair_id, "updated": time.time()})
    _trim_log_file(pair_log_path(pair_id), 300_000)
    summary = {"copied": copied, "bytes": last_bytes, "seconds": round(time.monotonic() - began)}
    return code, "\n".join(errors[-5:]), summary


def human_size(num: float) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if num < 1024 or unit == "TiB":
            return f"{num:.0f} {unit}" if unit == "B" else f"{num:.1f} {unit}"
        num /= 1024
    return f"{num:.1f} TiB"


def drive_usage(remote: str = DEFAULT_REMOTE) -> dict | None:
    """Return {"total", "used", "free"} in bytes for the whole cloud account, or None if unavailable."""
    result = run_rclone("about", f"{remote}:", "--json", timeout=60)
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
        return {"total": data["total"], "used": data["used"], "free": data["free"]}
    except (ValueError, KeyError):
        return None


def measure_pair(settings: dict, pair: dict) -> None:
    """Store how many files and bytes this pair holds in the cloud (listing is slow, so callers rate-limit it)."""
    try:
        destination = pair_remote(settings, pair)
    except RuntimeError:
        return
    result = run_rclone("size", destination, "--json", timeout=600)
    if result.returncode != 0:
        return
    try:
        data = json.loads(result.stdout)
        pair["files"], pair["bytes"] = data["count"], data["bytes"]
        pair["size_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    except (ValueError, KeyError):
        pass


def _size_is_stale(pair: dict) -> bool:
    stamp = pair.get("size_at")
    if not stamp:
        return True
    return (datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds() > 3600


def notify(title: str, body: str) -> None:
    """Best-effort desktop notification; the timer runs without a window."""
    if shutil.which("notify-send"):
        subprocess.run(["notify-send", "--app-name", APP_NAME, title, body], check=False)


def sync_once() -> int:
    ensure_private_dir()
    with LOCK_FILE.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        settings = load_settings()
        remotes = list_remotes()
        failed = False
        for pair in settings["pairs"]:
            account = get_account(settings, pair.get("account"))
            if not pair.get("enabled") or not pair.get("approved") or not account or account["remote"] not in remotes:
                continue
            code, output = sync_pair(settings, pair)
            pair["last_sync"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            pair["last_status"] = "Synced" if code == 0 else "Error: " + output[-240:].strip()
            failed = failed or code != 0
            if code == 0 and _size_is_stale(pair):
                measure_pair(settings, pair)
            if code != 0 and "invalid_grant" in output:
                name = PROVIDERS.get(account["provider"], {}).get("name", "Cloud")
                pair["last_status"] = f"{name} login expired. Open the app and sign in again."
                account["needs_signin"] = True
                notify(APP_NAME, f"{name} login expired. Open the app and sign in again.")
            save_settings(settings)
        return 1 if failed else 0


def install_user_service() -> tuple[bool, str]:
    unit_dir = Path.home() / ".config/systemd/user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    service = unit_dir / "gdrive-folder-backup.service"
    timer = unit_dir / "gdrive-folder-backup.timer"
    service.write_text(
        "[Unit]\nDescription=Ubuntu Files Cloud Backup\n\n[Service]\nType=oneshot\n"
        f"ExecStart=/usr/bin/python3 {PROJECT_FILE} --sync-once\n"
        f"WorkingDirectory={PROJECT_FILE.parent}\n"
    )
    timer.write_text(
        "[Unit]\nDescription=Check selected folders for cloud backup\n\n"
        "[Timer]\nOnBootSec=2min\nOnUnitActiveSec=5min\nPersistent=true\n"
        "Unit=gdrive-folder-backup.service\n\n[Install]\nWantedBy=timers.target\n"
    )
    for file in (service, timer):
        os.chmod(file, 0o600)
    reload_result = subprocess.run(["systemctl", "--user", "daemon-reload"], text=True,
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if reload_result.returncode:
        return False, reload_result.stdout
    result = subprocess.run(["systemctl", "--user", "enable", "--now", timer.name], text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return result.returncode == 0, result.stdout


