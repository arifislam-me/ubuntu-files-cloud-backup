"""One-click Google sign-in (OAuth 2.0 loopback + PKCE). Writes the token into the app's rclone config."""
from __future__ import annotations

import base64
import configparser
import hashlib
import html
import json
import os
import secrets
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from engine import APP_DIR, DEFAULT_REMOTE, RCLONE_CONFIG, delete_remote, ensure_private_dir

CLIENT_FILE = APP_DIR / "oauth-client.json"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
REVOKE_URL = "https://oauth2.googleapis.com/revoke"
# drive.file: only files this app creates. openid+email: show which account is connected.
SCOPES = "https://www.googleapis.com/auth/drive.file openid email"
LOGIN_TIMEOUT = 300


class LoginError(Exception):
    pass


def load_client() -> tuple[str, str]:
    """Return (client_id, client_secret) from oauth-client.json or the environment."""
    cid, secret = os.environ.get("GDRIVE_BACKUP_CLIENT_ID"), os.environ.get("GDRIVE_BACKUP_CLIENT_SECRET")
    if cid and secret:
        return cid, secret
    try:
        data = json.loads(CLIENT_FILE.read_text())
        block = data.get("installed") or data.get("web") or data
        return block["client_id"], block["client_secret"]
    except (OSError, ValueError, KeyError) as exc:
        raise LoginError(
            f"Google OAuth client file not found or invalid: {CLIENT_FILE}\n"
            "Create a Desktop-app OAuth client (see oauth-setup.md) and save its JSON there."
        ) from exc


def _page(message: str) -> bytes:
    body = (
        "<!doctype html><meta charset=utf-8><title>Ubuntu Files Cloud Backup</title>"
        "<body style='font-family:sans-serif;text-align:center;margin-top:18vh;color:#222'>"
        f"<h2>{html.escape(message)}</h2><p>You can close this tab and go back to the app.</p></body>"
    )
    return body.encode()


class _Handler(BaseHTTPRequestHandler):
    """Receives Google's redirect on 127.0.0.1 and stores the result on the server object."""

    def do_GET(self):  # noqa: N802
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if "code" not in query and "error" not in query:
            self.send_response(404)
            self.end_headers()
            return
        result = self.server.result
        if query.get("state", [""])[0] != self.server.state:
            result["error"] = "State mismatch"
            message = "Sign-in failed"
        elif "error" in query:
            result["error"] = query["error"][0]
            message = "Sign-in was cancelled"
        else:
            result["code"] = query["code"][0]
            message = "Signed in to Ubuntu Files Cloud Backup"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(_page(message))

    def log_message(self, *_args):
        pass


def _wait_for_code(server: HTTPServer) -> str:
    deadline = time.monotonic() + LOGIN_TIMEOUT
    server.timeout = 2
    while not server.result and time.monotonic() < deadline:
        server.handle_request()
    if "code" in server.result:
        return server.result["code"]
    raise LoginError(server.result.get("error") or "Sign-in timed out. Try again.")


def _post(url: str, fields: dict[str, str]) -> dict:
    req = urllib.request.Request(url, data=urllib.parse.urlencode(fields).encode(), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        raise LoginError(f"Google refused the request ({exc.code}): {detail}") from exc
    except OSError as exc:
        raise LoginError(f"Could not reach Google: {exc}") from exc


def _email_from_id_token(id_token: str) -> str:
    try:
        payload = id_token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("email", "")
    except (IndexError, ValueError):
        return ""


def write_remote(client_id: str, client_secret: str, token: dict, name: str = DEFAULT_REMOTE) -> None:
    ensure_private_dir()
    expiry = datetime.now(timezone.utc) + timedelta(seconds=int(token.get("expires_in", 3600)))
    rclone_token = {
        "access_token": token["access_token"],
        "token_type": "Bearer",
        "refresh_token": token["refresh_token"],
        "expiry": expiry.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
    }
    conf = configparser.ConfigParser(interpolation=None)
    conf.read(RCLONE_CONFIG)
    conf[name] = {"type": "drive", "client_id": client_id, "client_secret": client_secret,
                  "scope": "drive.file", "token": json.dumps(rclone_token, separators=(",", ":"))}
    tmp = RCLONE_CONFIG.with_suffix(".tmp")
    with tmp.open("w") as handle:
        conf.write(handle)
    os.chmod(tmp, 0o600)
    tmp.replace(RCLONE_CONFIG)


def sign_in(remote: str = DEFAULT_REMOTE) -> str:
    """Open the browser, wait for the user to approve, store the token. Returns the account email."""
    client_id, client_secret = load_client()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    server.result, server.state = {}, state
    redirect = f"http://127.0.0.1:{server.server_port}"
    query = urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
        "scope": SCOPES, "state": state, "code_challenge": challenge,
        "code_challenge_method": "S256", "access_type": "offline", "prompt": "consent",
    })
    try:
        subprocess.Popen(["xdg-open", f"{AUTH_URL}?{query}"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        code = _wait_for_code(server)
    finally:
        server.server_close()
    token = _post(TOKEN_URL, {
        "code": code, "client_id": client_id, "client_secret": client_secret,
        "redirect_uri": redirect, "grant_type": "authorization_code", "code_verifier": verifier,
    })
    if "refresh_token" not in token:
        raise LoginError("Google did not return a refresh token. Remove the app at myaccount.google.com/permissions and try again.")
    write_remote(client_id, client_secret, token, remote)
    return _email_from_id_token(token.get("id_token", ""))


def sign_out(remote: str = DEFAULT_REMOTE) -> None:
    """Revoke the token at Google (best effort) and remove this account's remote from the rclone config."""
    try:
        conf = configparser.ConfigParser(interpolation=None)
        conf.read(RCLONE_CONFIG)
        refresh = json.loads(conf[remote]["token"]).get("refresh_token")
        if refresh:
            _post(REVOKE_URL, {"token": refresh})
    except (OSError, ValueError, KeyError, LoginError):
        pass
    delete_remote(remote)
