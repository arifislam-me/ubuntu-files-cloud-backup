import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import engine  # noqa: E402
import oauth  # noqa: E402


class ValidatePairTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "a").mkdir()
        (self.root / "a" / "inner").mkdir()
        (self.root / "b").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_accepts_plain_folder(self):
        self.assertIsNone(engine.validate_pair(str(self.root / "a"), "A", []))

    def test_rejects_missing_folder(self):
        self.assertIn("existing", engine.validate_pair(str(self.root / "nope"), "X", []))

    def test_rejects_empty_or_unsafe_destination(self):
        self.assertIn("destination", engine.validate_pair(str(self.root / "a"), " / ", []))
        self.assertIn("relative", engine.validate_pair(str(self.root / "a"), "../x", []))
        self.assertIn("relative", engine.validate_pair(str(self.root / "a"), "x:y", []))

    def test_rejects_overlapping_local_folders(self):
        pairs = [{"id": "1", "local_path": str(self.root / "a"), "remote_path": "A"}]
        self.assertIn("overlaps", engine.validate_pair(str(self.root / "a" / "inner"), "I", pairs))
        self.assertIn("overlaps", engine.validate_pair(str(self.root), "R", pairs))

    def test_rejects_overlapping_destinations(self):
        pairs = [{"id": "1", "local_path": str(self.root / "a"), "remote_path": "Docs"}]
        self.assertIn("overlap", engine.validate_pair(str(self.root / "b"), "Docs/sub", pairs))


class SkipScanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        for rel in ("a/.venv/lib", "b/.venv", "a/src/__pycache__", "node_modules/m", "a/build"):
            (self.root / rel).mkdir(parents=True)
        (self.root / "a/.venv/lib/f.py").write_text("x" * 100)

    def tearDown(self):
        self.tmp.cleanup()

    def test_scan_finds_nested_groups_with_sizes(self):
        groups = engine.scan_skippable(str(self.root))["groups"]
        self.assertEqual({p["path"] for p in groups[".venv"]}, {"a/.venv", "b/.venv"})
        self.assertEqual(next(p for p in groups[".venv"] if p["path"] == "a/.venv")["bytes"], 100)
        self.assertIn("node_modules", groups)
        self.assertIn("build", groups)

    def test_scan_does_not_descend_into_found_folder(self):
        (self.root / "a/.venv/lib/node_modules").mkdir()
        groups = engine.scan_skippable(str(self.root))["groups"]
        self.assertEqual([p["path"] for p in groups["node_modules"]], ["node_modules"])

    def test_rules_for_names_and_paths(self):
        rules = engine.build_excludes({"skip": {"names": [".venv"], "paths": ["a/build"]}}, self.root)
        self.assertEqual(rules, [".venv/**", "/a/build/**"])

    def test_rules_escape_glob_characters(self):
        rules = engine.build_excludes({"skip": {"paths": ["we[ir]d*dir"]}}, self.root)
        self.assertEqual(rules, ["/we\\[ir\\]d\\*dir/**"])

    def test_no_skip_means_no_rules(self):
        self.assertEqual(engine.build_excludes({}, self.root), [])


class OAuthTests(unittest.TestCase):
    def test_write_remote_is_private_and_has_token(self):
        with tempfile.TemporaryDirectory() as tmp:
            conf = Path(tmp) / "rclone.conf"
            with mock.patch.object(oauth, "RCLONE_CONFIG", conf), mock.patch.object(oauth, "ensure_private_dir"):
                oauth.write_remote("id", "secret", {"access_token": "a", "refresh_token": "r", "expires_in": 3600}, "gdrive-x")
            text = conf.read_text()
            self.assertIn("scope = drive.file", text)
            token = json.loads(next(l for l in text.splitlines() if l.startswith("token = "))[8:])
            self.assertEqual(token["refresh_token"], "r")
            self.assertEqual(oct(conf.stat().st_mode & 0o777), "0o600")

    def test_load_client_reads_installed_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            f = Path(tmp) / "c.json"
            f.write_text(json.dumps({"installed": {"client_id": "i", "client_secret": "s"}}))
            with mock.patch.object(oauth, "CLIENT_FILE", f), mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("GDRIVE_BACKUP_CLIENT_ID", None)
                self.assertEqual(oauth.load_client(), ("i", "s"))

    def test_load_client_missing_file_explains(self):
        with mock.patch.object(oauth, "CLIENT_FILE", Path("/nonexistent/x.json")):
            os.environ.pop("GDRIVE_BACKUP_CLIENT_ID", None)
            with self.assertRaises(oauth.LoginError):
                oauth.load_client()

    def test_email_from_id_token(self):
        import base64
        payload = base64.urlsafe_b64encode(json.dumps({"email": "a@b.c"}).encode()).rstrip(b"=").decode()
        self.assertEqual(oauth._email_from_id_token(f"x.{payload}.y"), "a@b.c")
        self.assertEqual(oauth._email_from_id_token("garbage"), "")


class SizeTests(unittest.TestCase):
    def test_human_size(self):
        self.assertEqual(engine.human_size(512), "512 B")
        self.assertEqual(engine.human_size(1536), "1.5 KiB")
        self.assertEqual(engine.human_size(5 * 1024**4), "5.0 TiB")

    def test_size_staleness(self):
        from datetime import datetime, timedelta, timezone
        fresh = datetime.now(timezone.utc).isoformat()
        old = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        self.assertTrue(engine._size_is_stale({}))
        self.assertFalse(engine._size_is_stale({"size_at": fresh}))
        self.assertTrue(engine._size_is_stale({"size_at": old}))


if __name__ == "__main__":
    unittest.main()
