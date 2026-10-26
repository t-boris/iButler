"""Fail closed after extension updates without stopping other channels."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import subprocess


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("preflight", ROOT / "preflight.py")
preflight = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preflight)
spec = importlib.util.spec_from_file_location("installer", ROOT / "install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class PreflightTests(unittest.TestCase):
    def run_preflight(self, *, result=0, enabled=True, receipts=False, raises=None):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            config = home / ".openclaw/openclaw.json"
            config.parent.mkdir()
            data = {"channels": {"telegram": {"enabled": True}, "whatsapp": {
                "enabled": enabled, "sendReadReceipts": receipts, "debounceMs": 0}},
                "session": {"sendPolicy": {"default": "allow", "rules": [
                    {"action": "deny", "match": {"channel": "whatsapp"}}]}}}
            config.write_text(json.dumps(data))
            original = config.read_bytes()
            with patch.object(Path, "home", return_value=home), \
                 patch.object(preflight.subprocess, "run", return_value=subprocess.CompletedProcess([], result),
                              side_effect=raises) as check:
                preflight.main()
            after = json.loads(config.read_text())
            self.assertTrue(after["channels"]["telegram"]["enabled"])
            return after["channels"]["whatsapp"]["enabled"], check.call_count, config.read_bytes() == original

    def test_valid_guards_leave_config_unchanged(self):
        self.assertEqual(self.run_preflight(), (True, 1, True))

    def test_missing_patch_disables_only_whatsapp(self):
        self.assertEqual(self.run_preflight(result=1), (False, 1, False))

    def test_timeout_disables_only_whatsapp(self):
        self.assertEqual(self.run_preflight(raises=subprocess.TimeoutExpired("check", 15)), (False, 1, False))

    def test_read_receipt_drift_disables_whatsapp(self):
        self.assertEqual(self.run_preflight(receipts=True), (False, 1, False))

    def test_already_disabled_skips_checks(self):
        self.assertEqual(self.run_preflight(enabled=False), (False, 0, True))

    def test_unknown_extension_layout_rejected_before_writes(self):
        with tempfile.TemporaryDirectory() as root:
            dist = Path(root)
            monitor = dist / "monitor-new.js"
            monitor.write_text("function createWebOnMessageHandler(params) { /* changed */ }")
            session = dist / "session-new.js"
            session.write_text("async function createWaSocket() { /* changed */ }")
            with self.assertRaises(ValueError):
                installer.prepare_patch(dist)
            self.assertEqual(list(dist.iterdir()), [monitor, session])
            self.assertEqual(monitor.read_text(), "function createWebOnMessageHandler(params) { /* changed */ }")


if __name__ == "__main__":
    unittest.main()
