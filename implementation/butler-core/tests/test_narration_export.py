"""W5 compatibility: the installed ``news_video`` asks the core before narration leaves the machine.

The installed library is copied next to a stand-in core client and driven by the
workspace interpreter. A refused or unreachable core degrades the episode to the
offline macOS voice; nothing is synthesized in these tests.
"""
import json
import subprocess
import unittest
from pathlib import Path

from butler_core.adapters import butler as workspace_adapters

INSTALLED = Path.home() / ".openclaw/workspace-butler"
PYTHON = workspace_adapters.WORKSPACE_PYTHON

FAKE_CLIENT = '''
import json
from pathlib import Path
HERE = Path(__file__).parent
MODE = (HERE / "mode").read_text().strip()
class CoreUnavailable(ConnectionError): pass
class CoreError(RuntimeError):
    def __init__(self, code, message): super().__init__(code); self.code, self.message = code, message
def switched(name): return MODE != "off" and name == "exports"
class Client:
    def execute(self, kind, persona, target, params, key, principal=None, timeout=60, task=None, wait=None):
        with (HERE / "calls.jsonl").open("a") as stream:
            stream.write(json.dumps({"kind": kind, "persona": persona, "target": target, "params": params, "key": key, "principal": principal}) + "\\n")
        if MODE == "down": raise CoreUnavailable("down")
        if MODE == "deny-edge" and kind.endswith(".edge"): return {"id": "op-e", "state": "denied", "reason": "no grant"}
        if MODE == "deny-all": return {"id": "op-d", "state": "denied", "reason": "no grant"}
        return {"id": "op-1", "state": "succeeded"}
'''

EPISODE = {"episode_id": "daily-2026-10-24-morning", "title": "t", "scenes": [{"id": "s1", "narration": "Доброе утро."},
                                                                           {"id": "s2", "narration": "Новости дня."}]}


def harness(root, mode):
    ws = root / "ws"
    (ws / "lib").mkdir(parents=True, exist_ok=True)
    (ws / "lib/__init__.py").write_text("")
    (ws / "lib/news_video.py").write_text((INSTALLED / "lib/news_video.py").read_text())
    (ws / "lib/butler_core_client.py").write_text(FAKE_CLIENT)
    (ws / "lib/mode").write_text(mode)
    (ws / "lib/calls.jsonl").write_text("")
    return ws


def authorize(ws, tts):
    code = ("import json, sys\nfrom lib import news_video\n"
            f"print(json.dumps(news_video.authorize_narration_export({tts!r}, 'cedar', json.loads(sys.stdin.read()))))")
    done = subprocess.run([str(PYTHON), "-c", code], input=json.dumps(EPISODE), capture_output=True, text=True,
                          env={"PYTHONPATH": str(ws), "HOME": str(Path.home()), "PATH": "/usr/bin:/bin"}, cwd=ws, timeout=120)
    if done.returncode:
        raise AssertionError(done.stderr[-800:])
    calls = [json.loads(line) for line in (ws / "lib/calls.jsonl").read_text().splitlines() if line]
    return json.loads(done.stdout), calls


class NarrationExportTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.root = Path(tempfile.mkdtemp(prefix="narration-"))

    def tearDown(self):
        import shutil
        shutil.rmtree(self.root, ignore_errors=True)

    def test_authorized_openai_narration_keeps_its_voice_and_records_the_export(self):
        result, calls = authorize(harness(self.root, "ok"), "openai")
        self.assertEqual(result[:3], ["openai", "cedar", True])
        self.assertEqual([c["kind"] for c in calls], ["export.narration.openai", "export.narration.edge"])
        self.assertEqual(calls[0]["params"], {"episode_id": "daily-2026-10-24-morning", "chars": 24})
        self.assertEqual((calls[0]["persona"], calls[0]["principal"]), ("butler", "agent:butler"))
        self.assertEqual(calls[0]["key"], "narration:daily-2026-10-24-morning:openai")

    def test_edge_denied_for_butler_only_blocks_the_edge_fallback(self):
        result, _ = authorize(harness(self.root, "deny-edge"), "openai")
        self.assertEqual(result[:3], ["openai", "cedar", False])
        self.assertIn("edge narration export not authorized", result[3][0])

    def test_denied_or_unreachable_core_renders_offline(self):
        for mode in ("deny-all", "down"):
            result, _ = authorize(harness(self.root, mode), "openai")
            self.assertEqual(result[0], "macos", mode)
            self.assertFalse(result[2])
            self.assertIn("offline macOS voice", result[3][0])

    def test_switch_off_and_local_voices_never_ask_the_core(self):
        result, calls = authorize(harness(self.root, "off"), "openai")
        self.assertEqual((result[0], calls), ("openai", []))
        result, calls = authorize(harness(self.root, "ok"), "macos")
        self.assertEqual((result[0], calls), ("macos", []))


if __name__ == "__main__":
    unittest.main()
