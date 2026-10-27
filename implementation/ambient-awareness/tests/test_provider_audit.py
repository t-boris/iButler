"""The no-API proof: only subscription providers may have answered, and the audit never exposes message text."""
import contextlib
import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from butler_ambient import provider_audit


def message(provider, model="m", api="a", text="секретный текст", role="assistant"):
    return {"type": "message", "message": {"role": role, "provider": provider, "api": api, "model": model,
                                           "content": [{"type": "text", "text": text}]}}


class ProviderAuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def session(self, name, rows, agent="butler", age_days=0, suffix=".jsonl"):
        folder = self.root / agent / "sessions"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / (name + suffix)
        path.write_text("\n".join(r if isinstance(r, str) else json.dumps(r) for r in rows) + "\n", encoding="utf-8")
        stamp = time.time() - age_days * 86400
        os.utime(path, (stamp, stamp))
        return path

    def run_main(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = provider_audit.main(["--agents-dir", str(self.root), *args])
        return code, out.getvalue()

    def test_subscription_providers_and_the_delivery_mirror_pass(self):
        self.session("a", [message("openai-codex"), message("claude-cli"), message("openclaw", "delivery-mirror")])
        code, text = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("only subscription providers", text)

    def test_an_api_provider_fails_the_audit_and_is_named(self):
        self.session("a", [message("openai-codex"), message("anthropic", "claude-sonnet-5", "anthropic-messages")])
        code, text = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("BAD", text)
        self.assertIn("anthropic / anthropic-messages / claude-sonnet-5", text)
        self.assertIn("1 message(s) from a provider that is not allowed", text)

    def test_openclaw_is_allowed_only_as_the_delivery_mirror(self):
        self.session("a", [message("openclaw", "something-else")])
        self.assertEqual(self.run_main()[0], 1)

    def test_message_text_is_never_printed(self):
        self.session("a", [message("openai-codex", text="секретный текст")])
        self.assertNotIn("секрет", self.run_main()[1])

    def test_only_assistant_messages_are_counted(self):
        self.session("a", [message("anthropic", role="user"), message("anthropic", role="toolResult"),
                           {"type": "custom"}, "not json", "[1, 2]", message("openai-codex")])
        counts, files = provider_audit.scan(self.root)
        self.assertEqual(dict(counts), {("openai-codex", "a", "m"): 1})
        self.assertEqual(files, 1)

    def test_old_sessions_and_trajectory_files_are_ignored(self):
        self.session("old", [message("anthropic")], age_days=30)
        self.session("t", [message("anthropic")], suffix=".trajectory.jsonl")
        self.session("new", [message("claude-cli")])
        counts, files = provider_audit.scan(self.root, days=4)
        self.assertEqual(dict(counts), {("claude-cli", "a", "m"): 1})
        self.assertEqual(files, 1)

    def test_every_agent_directory_is_scanned(self):
        self.session("a", [message("openai-codex")], agent="butler")
        self.session("b", [message("anthropic")], agent="tetyana-dyck")
        self.assertEqual(self.run_main()[0], 1)

    def test_a_missing_directory_is_an_empty_pass_not_a_crash(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = provider_audit.main(["--agents-dir", str(self.root / "nowhere")])
        self.assertEqual(code, 0)
        self.assertIn("0 session files", out.getvalue())


if __name__ == "__main__":
    unittest.main()
