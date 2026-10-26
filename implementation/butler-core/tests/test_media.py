"""W5: YouTube uploads through the workspace CLI, reconciled from the channel; exports recorded before they happen."""
import json
import os
import stat
import unittest
from pathlib import Path

from butler_core.adapters.exports import ExportAdapter
from butler_core.adapters.youtube import YouTubeAdapter
from butler_core.service import Core
from support import CoreCase, base_config

FAKE_CLI = r'''#!/usr/bin/env python3
"""A stand-in for bin/butler-youtube: writes the same receipts, behaves as the mode file says."""
import json, sys, time
from pathlib import Path
here = Path(__file__).resolve().parent.parent
mode = (here / "mode").read_text().strip() if (here / "mode").exists() else "ok"
args = sys.argv[1:]
(here / "calls.jsonl").open("a").write(json.dumps({"args": args, "env": {k: v for k, v in __import__("os").environ.items() if k.startswith("BUTLER_")}}) + "\n")
def opt(name, default=None):
    return args[args.index(name) + 1] if name in args else default
if args[0] in ("upload", "upload-file"):
    target = Path(args[1])
    receipt_path = target / "publish" / "youtube.json" if args[0] == "upload" else target.with_suffix(target.suffix + ".youtube.json")
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    title = opt("--title", "Episode title")
    if mode == "refuse":
        print("error: ValidationError: rendered video checksum does not match run.json", file=sys.stderr); sys.exit(1)
    if mode == "not-started":
        print("error: upload session init failed (503)", file=sys.stderr); sys.exit(1)
    receipt_path.write_text(json.dumps({"status": "pending", "started_at_ts": time.time(), "title": title,
                                        "approval": "core:" + opt("--core-operation", "")}))
    if mode == "die":
        sys.exit(137)
    receipt = {"status": "published", "video_id": "vid-1", "url": "https://youtu.be/vid-1", "api_privacy_status": "private",
               "privacy": opt("--privacy"), "approval": "core:" + opt("--core-operation", "")}
    receipt_path.write_text(json.dumps(receipt)); print(json.dumps(receipt)); sys.exit(0)
if args[0] == "find":
    found = (here / "found").exists()
    if found:
        payload = {"video_id": "vid-9", "url": "https://youtu.be/vid-9", "api_privacy_status": "private"}
        Path(opt("--write-receipt")).write_text(json.dumps({"status": "published", **payload}))
        print(json.dumps(payload))
    else:
        print("{}")
    sys.exit(0)
sys.exit(2)
'''


def media_config():
    config = base_config()
    publish = {"adapter": "youtube", "reconcile": "auto", "max_attempts": 2, "timeout": 1900,
               "export": [{"category": "video", "recipient": "youtube"}]}
    config["operations"]["youtube.publish_news"] = {**publish, "approval": "none"}
    config["operations"]["youtube.publish"] = {**publish, "approval": "required"}
    config["operations"]["export.narration.openai"] = {"adapter": "export", "approval": "none", "max_attempts": 1,
                                                        "export": [{"category": "narration-text", "recipient": "openai"}]}
    config["operations"]["export.narration.edge"] = {"adapter": "export", "approval": "none", "max_attempts": 1,
                                                      "export": [{"category": "narration-text", "recipient": "microsoft-edge-tts"}]}
    config["personas"]["butler"]["actions"] += ["youtube.publish_news", "youtube.publish", "export.narration.openai",
                                                 "export.narration.edge"]
    config["personas"]["bamberg-news"] = {"principals": [], "sources": [], "actions": ["youtube.publish_news", "export.narration.edge"]}
    config["exports"] += [{"persona": "butler", "category": "video", "recipient": "youtube"},
                          {"persona": "butler", "category": "narration-text", "recipient": "openai"},
                          {"persona": "bamberg-news", "category": "video", "recipient": "youtube"},
                          {"persona": "bamberg-news", "category": "narration-text", "recipient": "microsoft-edge-tts"}]
    config["limits"]["workers"].update({"youtube": 1, "export": 1})
    return config


class MediaCase(CoreCase):
    def make_core(self, **kwargs):
        self.ws = self.tmp / "ws"
        (self.ws / "bin").mkdir(parents=True)
        cli = self.ws / "bin/butler-youtube"
        cli.write_text(FAKE_CLI)
        cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
        self.youtube = YouTubeAdapter(self.ws, os.environ.get("PATH", "/usr/bin:/bin"), {"TMPDIR": "/tmp"}, clock=self.clock)
        return Core(self.tmp / "state", adapters={"test": self.adapter, "youtube": self.youtube, "export": ExportAdapter()},
                    transports={"telegram:butler": self.transport}, clock=self.clock, reserve=0)

    def setUp(self):
        super().setUp()
        self.activate(media_config())
        self.run_dir = self.tmp / "runs/daily-2026-10-24-morning"
        (self.run_dir / "render").mkdir(parents=True)

    def mode(self, value):
        (self.ws / "mode").write_text(value)

    def calls(self):
        path = self.ws / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def submit(self, kind="youtube.publish_news", key="y1", persona="butler", **params):
        params = {"run_dir": str(self.run_dir), "privacy": "public", "profile": "default",
                  "credentials_dir": "/tmp/creds", "runs_root": str(self.tmp / "runs"), "title": "Episode title", **params}
        return self.core.submit({"kind": kind, "persona": persona, "principal": f"agent:{persona}", "target": "youtube:default",
                                 "params": params, "idempotency_key": key}, "system2")


class YouTubeTest(MediaCase):
    def test_news_upload_runs_the_cli_with_the_persona_credentials_only(self):
        op = self.submit()
        self.assertEqual(op["state"], "requested")
        self.assertEqual(self.core.operations.run(op["id"]), "succeeded")
        record = self.core.operations.get(op["id"])
        self.assertEqual(record["receipt"], "youtube:video:vid-1")
        self.assertEqual(json.loads(record["result"])["url"], "https://youtu.be/vid-1")
        call = self.calls()[0]
        self.assertEqual(call["args"][:2], ["upload", str(self.run_dir)])
        self.assertIn("--core-operation", call["args"])
        self.assertEqual(call["env"]["BUTLER_YOUTUBE_CREDENTIALS"], "/tmp/creds")
        self.assertEqual(call["env"]["BUTLER_NEWS_VIDEO_RUNS"], str(self.tmp / "runs"))
        self.assertNotIn("BUTLER_BOT_TOKEN", call["env"])

    def test_per_video_uploads_wait_for_the_owner(self):
        op = self.submit(kind="youtube.publish", key="y2")
        self.assertEqual(op["state"], "awaiting_approval")
        self.assertEqual(self.calls(), [])
        self.core.approvals.grant(op["id"], op["review_code"], "terminal:owner", "terminal")
        self.assertEqual(self.core.operations.run(op["id"]), "succeeded")
        self.assertEqual(len(self.calls()), 1)

    def test_cli_killed_after_the_upload_is_reconciled_from_the_channel_never_re_uploaded(self):
        self.mode("die")
        op = self.submit(key="y3")
        self.assertEqual(self.core.operations.run(op["id"]), "needs_reconciliation")     # listing may lag: unknown
        uploads = [c for c in self.calls() if c["args"][0] == "upload"]
        self.assertEqual(len(uploads), 1)
        (self.ws / "found").write_text("")
        self.assertEqual(self.core.operations.reconcile(op["id"]), "succeeded")
        self.assertEqual(self.core.operations.get(op["id"])["receipt"], "youtube:video:vid-9")
        self.assertEqual(json.loads((self.run_dir / "publish/youtube.json").read_text())["status"], "published")
        self.assertEqual(len([c for c in self.calls() if c["args"][0] == "upload"]), 1)

    def test_old_pending_upload_absent_from_the_channel_is_retried(self):
        self.mode("die")
        op = self.submit(key="y4")
        self.assertEqual(self.core.operations.run(op["id"]), "needs_reconciliation")
        # The fake CLI stamps wall-clock time; move the pending receipt into the test clock's past.
        receipt_path = self.run_dir / "publish/youtube.json"
        receipt = json.loads(receipt_path.read_text())
        receipt["started_at_ts"] = self.clock() - 21 * 60
        receipt_path.write_text(json.dumps(receipt))
        self.mode("ok")
        self.assertEqual(self.core.operations.reconcile(op["id"]), "requested")
        self.assertEqual(self.core.operations.run(op["id"]), "succeeded")
        self.assertEqual(len([c for c in self.calls() if c["args"][0] == "upload"]), 2)

    def test_refusals_and_failed_session_init(self):
        self.mode("refuse")
        op = self.submit(key="y5")
        self.assertEqual(self.core.operations.run(op["id"]), "failed")
        self.assertIn("checksum", self.core.operations.get(op["id"])["reason"])
        self.mode("not-started")
        op = self.submit(key="y6")
        self.assertEqual(self.core.operations.run(op["id"]), "requested")       # ambiguous, but no pending receipt: retry
        self.mode("ok")
        self.assertEqual(self.core.operations.run(op["id"]), "succeeded")

    def test_bamberg_persona_publishes_news_with_its_own_credentials(self):
        op = self.submit(key="y7", persona="bamberg-news", credentials_dir="/tmp/bamberg-creds", profile="bamberg")
        self.assertEqual(self.core.operations.run(op["id"]), "succeeded")
        self.assertEqual(self.calls()[0]["env"]["BUTLER_YOUTUBE_CREDENTIALS"], "/tmp/bamberg-creds")
        denied = self.submit(kind="youtube.publish", key="y8", persona="bamberg-news")
        self.assertEqual(denied["state"], "denied")


class ExportTest(MediaCase):
    def test_exports_are_recorded_and_policy_checked_before_anything_leaves(self):
        op = self.core.submit({"kind": "export.narration.openai", "persona": "butler", "principal": "agent:butler",
                               "target": "tts:openai", "params": {"episode_id": "e1", "chars": 1200},
                               "idempotency_key": "narration:e1:openai"}, "system2")
        self.assertEqual(self.core.operations.run(op["id"]), "succeeded")
        self.assertEqual(self.core.operations.get(op["id"])["receipt"], "export:narration-text:narration:e1:openai")
        edge = self.core.submit({"kind": "export.narration.edge", "persona": "butler", "principal": "agent:butler",
                                 "target": "tts:edge", "params": {"episode_id": "e1", "chars": 1200},
                                 "idempotency_key": "narration:e1:edge"}, "system2")
        self.assertEqual(edge["state"], "denied")           # Butler has no grant for Microsoft narration
        bamberg = self.core.submit({"kind": "export.narration.edge", "persona": "bamberg-news", "principal": "agent:bamberg-news",
                                    "target": "tts:edge", "params": {"episode_id": "b1", "chars": 900},
                                    "idempotency_key": "narration:b1:edge"}, "system2")
        self.assertEqual(self.core.operations.run(bamberg["id"]), "succeeded")
        bad = self.core.submit({"kind": "export.narration.openai", "persona": "butler", "principal": "agent:butler",
                                "target": "tts:openai", "params": {"episode_id": "e2"}, "idempotency_key": "narration:e2"}, "system2")
        self.assertEqual(self.core.operations.run(bad["id"]), "failed")


if __name__ == "__main__":
    unittest.main()
