"""W5 compatibility: the installed ``butler-youtube`` CLI on the core path (DEC-064).

The harness copies the installed CLI into a temporary workspace next to a stand-in
``lib/youtube.py`` that writes the same receipts and never talks to Google. The
core runs that same CLI again with ``--core-operation``; the test observes that a
daily run uploads under the standing authorization, a research run waits for the
owner, and the legacy path still runs when the switch is off.
"""
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from butler_core.adapters.youtube import YouTubeAdapter
from butler_core.server import Server
from butler_core.service import Core
from support import CoreCase
from test_dispatch import substitute
from test_media import media_config
from test_runtime import short_dir

INSTALLED = Path.home() / ".openclaw/workspace-butler"
CLIENT = Path(__file__).resolve().parents[1] / "butler_core/client.py"

FAKE_LIBRARY = '''"""Stand-in for lib/youtube.py: receipts as the real library writes them, no network."""
import json, os, time
from pathlib import Path

CREDENTIALS_DIR = Path(os.environ.get("BUTLER_YOUTUBE_CREDENTIALS", "{creds}"))
CLIENT_SECRET_PATH = CREDENTIALS_DIR / "client_secret.json"
MAX_TITLE_CHARS = 100


class NewsVideoError(Exception):
    pass


class YouTubeError(NewsVideoError):
    pass


class ValidationError(NewsVideoError):
    pass


class _NewsVideo:
    NewsVideoError = NewsVideoError
    RUNS_ROOT = Path(os.environ.get("BUTLER_NEWS_VIDEO_RUNS", "{runs}"))


news_video = _NewsVideo()


def _token_path(profile="default"):
    return CREDENTIALS_DIR / "token.json"


def _log(entry):
    with (Path("{ws}") / "library-calls.jsonl").open("a") as stream:
        stream.write(json.dumps(entry) + "\\n")


def publish_run_to_youtube(run_dir, *, approval, privacy="public", title=None, description=None, dry_run=False, profile="default"):
    run = Path(run_dir)
    episode = json.loads((run / "episode.json").read_text())
    receipt_path = run / "publish" / "youtube.json"
    if receipt_path.is_file() and json.loads(receipt_path.read_text()).get("status") == "published":
        return json.loads(receipt_path.read_text())
    if dry_run:
        return {{"status": "dry-run", "title": title or episode["title"]}}
    if not approval:
        raise ValidationError("YouTube upload requires an explicit Boris approval reference (--approve-upload)")
    _log({{"upload": str(run), "approval": approval, "privacy": privacy, "profile": profile, "credentials": str(CREDENTIALS_DIR)}})
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    receipt = {{"status": "published", "video_id": "vid-" + episode["episode_id"], "url": "https://youtu.be/vid-" + episode["episode_id"],
               "approval": approval, "privacy": privacy, "api_privacy_status": "private", "started_at_ts": time.time(),
               "title": title or episode["title"]}}
    receipt_path.write_text(json.dumps(receipt))
    return receipt


def upload_arbitrary_file(path, *, title, approval, description="", privacy="public", dry_run=False, profile="default"):
    if not approval:
        raise ValidationError("YouTube upload requires an explicit Boris approval reference (--approve-upload)")
    _log({{"upload_file": str(path), "approval": approval}})
    return {{"status": "published", "video_id": "vid-file", "url": "https://youtu.be/vid-file", "approval": approval}}


def find_upload(title, *, since_ts=0.0, profile="default"):
    return None


def record_found_upload(receipt_path, found):
    return found


def configure_destination(channel_id, *, profile="default"):
    return {{"channel_id": channel_id}}


def device_auth(profile="default"):
    raise YouTubeError("no device auth in tests")


def channel_info(profile="default"):
    return {{"channel_id": "UC-test", "title": "Test channel"}}
'''


def fake_workspace(root):
    ws = (root / "ws").resolve()
    (ws / "bin").mkdir(parents=True)
    (ws / "lib").mkdir()
    (ws / "runs").mkdir()
    (ws / "creds").mkdir()
    cli = substitute((INSTALLED / "bin/butler-youtube").read_text(), 'WORKSPACE = Path("/Users/boris/.openclaw/workspace-butler")',
                     f'WORKSPACE = Path("{ws}")')
    (ws / "bin/butler-youtube").write_text(cli)
    (ws / "bin/butler-youtube").chmod(0o755)
    (ws / "lib/youtube.py").write_text(FAKE_LIBRARY.format(creds=ws / "creds", runs=ws / "runs", ws=ws))
    (ws / "lib/__init__.py").write_text("")
    client = substitute(CLIENT.read_text(), "AVAILABILITY_WAIT = 30.0", "AVAILABILITY_WAIT = 2.0")
    (ws / "lib/butler_core_client.py").write_text(client)
    return ws


def make_run(ws, episode_id, profile):
    run = ws / "runs" / episode_id
    (run / "render").mkdir(parents=True)
    (run / "render/episode.mp4").write_bytes(b"\x00\x00video " + episode_id.encode())
    (run / "episode.json").write_text(json.dumps({"episode_id": episode_id, "title": f"Episode {episode_id}", "profile": profile}))
    return run


class YouTubeCLITest(CoreCase):
    def make_core(self, **kwargs):
        self.ws = fake_workspace(self.tmp)
        path_env = os.environ.get("PATH", "/usr/bin:/bin")
        self.youtube = YouTubeAdapter(self.ws, path_env, {"TMPDIR": "/tmp"}, clock=self.clock)
        from butler_core.adapters.exports import ExportAdapter
        return Core(self.tmp / "state", adapters={"test": self.adapter, "youtube": self.youtube, "export": ExportAdapter()},
                    transports={"telegram:butler": self.transport}, clock=self.clock, reserve=0)

    def setUp(self):
        super().setUp()
        config = media_config()
        config["scenarios"]["youtube"] = {"enabled": True}
        self.activate(config)
        self.sockdir = short_dir()
        self.server = Server(self.core, self.sockdir / "core.sock", classify=lambda pid: "local")
        self.server.start()
        self.core.start()

    def tearDown(self):
        self.server.stop()
        shutil.rmtree(self.sockdir, ignore_errors=True)
        super().tearDown()

    def cli(self, *arguments, switched=True, timeout=120):
        env = {**os.environ, "PYTHONPATH": str(self.ws), "BUTLER_CORE_STATE": str(self.core.store.root),
               "BUTLER_CORE_SOCKET": str(self.sockdir / "core.sock"), "BUTLER_YOUTUBE_CREDENTIALS": str(self.ws / "creds"),
               "BUTLER_NEWS_VIDEO_RUNS": str(self.ws / "runs")}
        if not switched:
            env["BUTLER_CORE_STATE"] = str(self.tmp / "no-core")
        return subprocess.run([sys.executable, str(self.ws / "bin/butler-youtube"), *arguments], capture_output=True,
                              text=True, env=env, cwd=self.ws, timeout=timeout)

    def library_calls(self):
        path = self.ws / "library-calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_daily_news_run_uploads_under_the_standing_authorization(self):
        run = make_run(self.ws, "daily-2026-10-24-morning", "daily")
        done = self.cli("upload", str(run))
        self.assertEqual(done.returncode, 0, done.stderr)
        payload = json.loads(done.stdout)
        self.assertEqual((payload["status"], payload["video_id"]), ("published", "vid-daily-2026-10-24-morning"))
        ops = self.core.operations.list(limit=5)
        self.assertEqual([(o["kind"], o["state"]) for o in ops], [("youtube.publish_news", "succeeded")])
        self.assertEqual(ops[0]["receipt"], "youtube:video:vid-daily-2026-10-24-morning")
        calls = self.library_calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["approval"], "core:" + ops[0]["id"])
        self.assertEqual(calls[0]["credentials"], str(self.ws / "creds"))
        # The same run again is the same operation: no second upload.
        again = self.cli("upload", str(run))
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(len(self.library_calls()), 1)

    def test_every_scheduled_news_profile_uploads_under_the_standing_authorization(self):
        # Butler's own episodes carry "butler-daily"; only "daily" was recognised until 2026-10-26, so the
        # first live evening upload was sent to the owner for approval instead of running.
        for profile in ("butler-daily", "bamberg-daily"):
            with self.subTest(profile=profile):
                run = make_run(self.ws, f"{profile}-2026-10-26-evening", profile)
                done = self.cli("upload", str(run))
                self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual([(o["kind"], o["state"]) for o in self.core.operations.list(limit=5)],
                         [("youtube.publish_news", "succeeded")] * 2)

    def test_an_operation_recorded_under_the_wrong_kind_does_not_block_the_corrected_upload(self):
        # The live case of 2026-10-26: the same file first became `youtube.publish` (waiting for the owner), then the
        # classification was fixed. The core refuses one key under different parameters, so the key carries the kind.
        run = make_run(self.ws, "butler-daily-2026-10-26-evening", "lecture")
        waiting = self.cli("upload", str(run))
        self.assertEqual(waiting.returncode, 4, waiting.stderr)
        stale = self.core.operations.list(limit=5)[0]
        self.core.operations.cancel(stale["id"], "terminal:owner")
        episode = json.loads((run / "episode.json").read_text())
        (run / "episode.json").write_text(json.dumps({**episode, "profile": "butler-daily"}))
        done = self.cli("upload", str(run))
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(sorted((o["kind"], o["state"]) for o in self.core.operations.list(limit=5)),
                         [("youtube.publish", "cancelled"), ("youtube.publish_news", "succeeded")])
        self.assertEqual(len(self.library_calls()), 1)

    def test_lecture_run_still_waits_for_the_owner(self):
        run = make_run(self.ws, "lecture-2026-10-26", "lecture")
        waiting = self.cli("upload", str(run))
        self.assertEqual(waiting.returncode, 4, waiting.stderr)
        self.assertEqual(self.library_calls(), [])
        self.assertEqual([(o["kind"], o["state"]) for o in self.core.operations.list(limit=5)],
                         [("youtube.publish", "awaiting_approval")])

    def test_research_run_waits_for_the_owner_then_uploads_once(self):
        run = make_run(self.ws, "deep-research-2026-10-24", "deep-research")
        waiting = self.cli("upload", str(run))
        self.assertEqual(waiting.returncode, 4, waiting.stderr)
        self.assertIn("подтверждения", waiting.stderr)
        self.assertEqual(self.library_calls(), [])
        pending = self.core.operations.list(limit=5)[0]
        self.assertEqual((pending["kind"], pending["state"]), ("youtube.publish", "awaiting_approval"))
        self.core.approvals.grant(pending["id"], pending["review_code"], "terminal:owner", "terminal")
        self.core.wake()
        self.core.wait(pending["id"], 60)
        self.assertEqual(self.core.operations.get(pending["id"])["state"], "succeeded")
        self.assertEqual(len(self.library_calls()), 1)
        settled = self.cli("upload", str(run))
        self.assertEqual(settled.returncode, 0, settled.stderr)
        self.assertEqual(json.loads(settled.stdout)["video_id"], "vid-deep-research-2026-10-24")
        self.assertEqual(len(self.library_calls()), 1)

    def test_switch_off_keeps_the_legacy_path_and_its_approval_reference(self):
        run = make_run(self.ws, "daily-2026-10-25-morning", "daily")
        refused = self.cli("upload", str(run), switched=False)
        self.assertEqual(refused.returncode, 1)
        self.assertIn("approve-upload", refused.stderr)
        done = self.cli("upload", str(run), "--approve-upload", "boris:2026-07-19:standing-auth-news-videos", switched=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.library_calls()[0]["approval"], "boris:2026-07-19:standing-auth-news-videos")
        self.assertEqual(self.core.operations.list(limit=5), [])

    def test_core_down_refuses_without_uploading(self):
        run = make_run(self.ws, "daily-2026-10-26-morning", "daily")
        self.server.stop()
        done = self.cli("upload", str(run))
        self.assertEqual(done.returncode, 3, done.stderr)
        self.assertEqual(self.library_calls(), [])


if __name__ == "__main__":
    unittest.main()
