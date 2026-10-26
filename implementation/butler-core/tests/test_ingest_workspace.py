"""W7 compatibility: the installed inbox buffer admits every message to the core's ingest store, text stays local.

The installed ``lib/inbox.py`` is copied with its workspace pointed at a temporary
folder and talks to a real core over its socket. The buffer file keeps the text;
the core's ingest store gets the stable key and metadata only.
"""
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from butler_core.server import Server
from support import CoreCase
from test_dispatch import substitute
from test_runtime import short_dir

INSTALLED = Path.home() / ".openclaw/workspace-butler"
CLIENT = Path(__file__).resolve().parents[1] / "butler_core/client.py"


def fake_workspace(root):
    ws = (root / "ws").resolve()
    (ws / "lib").mkdir(parents=True)
    source = substitute((INSTALLED / "lib/inbox.py").read_text(), 'WORKSPACE = Path("/Users/boris/.openclaw/workspace-butler")',
                        f'WORKSPACE = Path("{ws}")')
    (ws / "lib/inbox.py").write_text(source)
    (ws / "lib/__init__.py").write_text("")
    client = substitute(CLIENT.read_text(), "AVAILABILITY_WAIT = 30.0", "AVAILABILITY_WAIT = 2.0")
    (ws / "lib/butler_core_client.py").write_text(client)
    return ws


class InboxIngestTest(CoreCase):
    def setUp(self):
        super().setUp()
        config = self.core.config.active()[1]
        config = {**config, "scenarios": {**config["scenarios"], "ingest": {"enabled": True}}}
        self.activate(config)
        self.ws = fake_workspace(self.tmp)
        self.sockdir = short_dir()
        self.server = Server(self.core, self.sockdir / "core.sock", classify=lambda pid: "local")
        self.server.start()

    def tearDown(self):
        self.server.stop()
        shutil.rmtree(self.sockdir, ignore_errors=True)
        super().tearDown()

    def library(self, code, switched=True):
        env = {**os.environ, "PYTHONPATH": str(self.ws), "BUTLER_CORE_STATE": str(self.core.store.root),
               "BUTLER_CORE_SOCKET": str(self.sockdir / "core.sock")}
        if not switched:
            env["BUTLER_CORE_STATE"] = str(self.tmp / "no-core")
        return subprocess.run([sys.executable, "-c", "from lib import inbox\n" + code], capture_output=True, text=True,
                              env=env, cwd=self.ws, timeout=60)

    def events(self):
        return [dict(r) for r in self.core.ingest_store.query("SELECT dedup_key, source, kind, privacy, payload FROM events")]

    def buffer_rows(self):
        path = self.ws / "state/inbox/buffer.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def test_message_is_buffered_locally_and_admitted_to_the_core_without_its_text(self):
        done = self.library("print(inbox.buffer_message('telegram', '-1001', 'Секретный текст от Анны', source_title='Чат', sender='Анна', msg_id=7))")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "True")
        rows = self.buffer_rows()
        self.assertEqual((rows[0]["text"], rows[0]["core_admitted"]), ("Секретный текст от Анны", True))
        events = self.events()
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0]["source"], events[0]["kind"], events[0]["privacy"]), ("ingest:telegram", "message", "raw_local_only"))
        self.assertEqual(events[0]["dedup_key"], rows[0]["key"])
        self.assertNotIn("Секретный", events[0]["payload"])
        self.assertNotIn("Анна", events[0]["payload"])
        self.assertEqual(json.loads(events[0]["payload"])["sender_present"], True)
        # The same message again: neither a second buffer row nor a second event.
        again = self.library("print(inbox.buffer_message('telegram', '-1001', 'Секретный текст от Анны', source_title='Чат', sender='Анна', msg_id=7))")
        self.assertEqual(again.stdout.strip(), "False")
        self.assertEqual(len(self.events()), 1)

    def test_core_down_keeps_the_message_and_the_next_call_catches_up(self):
        self.server.stop()
        done = self.library("print(inbox.buffer_message('whatsapp', '1555', 'пока ядро лежит', msg_id='w1'))")
        self.assertEqual(done.stdout.strip(), "True", done.stderr)
        self.assertEqual(self.buffer_rows()[0]["core_admitted"], False)
        self.assertEqual(self.events(), [])
        self.server = Server(self.core, self.sockdir / "core.sock", classify=lambda pid: "local")
        self.server.start()
        listed = self.library("print(len(inbox.pending()))")
        self.assertEqual(listed.stdout.strip(), "1", listed.stderr)
        self.assertEqual(self.buffer_rows()[0]["core_admitted"], True)
        self.assertEqual([e["kind"] for e in self.events()], ["message"])
        marked = self.library("print(inbox.mark_processed([r['key'] for r in inbox.pending()]))")
        self.assertEqual(marked.stdout.strip(), "1", marked.stderr)
        self.assertEqual(sorted(e["kind"] for e in self.events()), ["message", "message.processed"])

    def test_switch_off_keeps_the_buffer_only(self):
        done = self.library("print(inbox.buffer_message('telegram', '-1002', 'без ядра', msg_id=1))", switched=False)
        self.assertEqual(done.stdout.strip(), "True", done.stderr)
        self.assertNotIn("core_admitted", self.buffer_rows()[0])
        self.assertEqual(self.events(), [])


if __name__ == "__main__":
    unittest.main()
