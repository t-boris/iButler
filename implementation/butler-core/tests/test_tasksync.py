"""W4 compatibility: the installed Butler task library writing through the core.

The harness copies the installed ``lib/tasksync.py`` and ``lib/todoist.py`` into
a temporary workspace and points only the API base, the token variable, the
retry pacing and the workspace path elsewhere; the vault mirror is a stub. The
core's adapter and the library talk to one fake Todoist over local HTTP, so a
duplicate task would be visible.
"""
import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

from butler_core.adapters.todoist import TodoistAdapter, TodoistClient
from butler_core.server import Server
from butler_core.service import Core
from fake_todoist import FakeTodoist
from support import CoreCase
from test_dispatch import substitute
from test_runtime import short_dir
from test_todoist import todoist_config

INSTALLED = Path.home() / ".openclaw/workspace-butler"
CLIENT = Path(__file__).resolve().parents[1] / "butler_core/client.py"


def fake_workspace(root, api_base):
    ws = (root / "ws").resolve()
    (ws / "lib").mkdir(parents=True)
    (ws / "state").mkdir()
    library = (INSTALLED / "lib/todoist.py").read_text()
    library = substitute(library, 'API_BASE = "https://api.todoist.com/api/v1"', f'API_BASE = "{api_base}"')
    library = substitute(library, 'TOKEN_VAR = "TODOIST_API_TOKEN"', 'TOKEN_VAR = "FAKE_TODOIST_TOKEN"')
    library = substitute(library, "_MAX_RETRIES = 5", "_MAX_RETRIES = 2")
    library = substitute(library, "_BACKOFF_BASE = 1.0", "_BACKOFF_BASE = 0.01")
    (ws / "lib/todoist.py").write_text(library)
    sync = (INSTALLED / "lib/tasksync.py").read_text()
    sync = substitute(sync, 'WORKSPACE = Path("/Users/boris/.openclaw/workspace-butler")', f'WORKSPACE = Path("{ws}")')
    (ws / "lib/tasksync.py").write_text(sync)
    (ws / "lib/vault.py").write_text("def edit(rel, body, source=None):\n    return None\n")
    (ws / "lib/__init__.py").write_text("")
    client = substitute(CLIENT.read_text(), "AVAILABILITY_WAIT = 30.0", "AVAILABILITY_WAIT = 2.0")
    (ws / "lib/butler_core_client.py").write_text(client)
    return ws


class TaskSyncTest(CoreCase):
    def make_core(self, **kwargs):
        self.todoist = FakeTodoist()
        self.api = self.todoist.serve()
        adapter = TodoistAdapter(TodoistClient(token=lambda: "core-token", base=self.api, timeout=5))
        return Core(self.tmp / "state", adapters={"test": self.adapter, "todoist": adapter},
                    transports={"telegram:butler": self.transport}, clock=self.clock, reserve=0)

    def setUp(self):
        super().setUp()
        config = todoist_config()
        config["scenarios"]["todoist"] = {"enabled": True}
        self.activate(config)
        self.ws = fake_workspace(self.tmp, self.api)
        self.sockdir = short_dir()
        self.server = Server(self.core, self.sockdir / "core.sock", classify=lambda pid: "local")
        self.server.start()
        self.core.start()

    def tearDown(self):
        self.server.stop()
        self.todoist.shutdown()
        shutil.rmtree(self.sockdir, ignore_errors=True)
        super().tearDown()

    def library(self, code, switched=True):
        env = {**os.environ, "PYTHONPATH": str(self.ws), "FAKE_TODOIST_TOKEN": "library-token",
               "BUTLER_CORE_STATE": str(self.core.store.root), "BUTLER_CORE_SOCKET": str(self.sockdir / "core.sock")}
        if not switched:
            env["BUTLER_CORE_STATE"] = str(self.tmp / "no-core")
        return subprocess.run([sys.executable, "-c", "from lib import tasksync as ts\n" + code], capture_output=True,
                              text=True, env=env, cwd=self.ws, timeout=60)

    def test_switched_add_task_is_one_core_operation_and_one_todoist_task(self):
        done = self.library("print(ts.add_task('Купить хлеб', due='tomorrow', priority=3)['id'])")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "101")
        ops = self.core.operations.list(limit=10)
        self.assertEqual([(o["kind"], o["state"]) for o in ops], [("todoist.create", "succeeded")])
        self.assertEqual(len(self.todoist.tasks), 1)
        syncs = [r for r in self.todoist.requests if r[1] == "/sync"]
        self.assertEqual(len(syncs), 1)
        self.assertTrue((self.ws / "state/todoist-cache.json").exists())      # the mirror still refreshes

    def test_switched_changes_go_through_the_core_and_delete_waits_for_the_owner(self):
        self.library("ts.add_task('Задача')")
        done = self.library("ts.complete('101'); ts.reopen('101'); ts.move_bucket('101', 'waiting'); ts.comment('101', 'note')")
        self.assertEqual(done.returncode, 0, done.stderr)
        kinds = sorted(o["kind"] for o in self.core.operations.list(limit=10))
        self.assertEqual(kinds, ["todoist.close", "todoist.comment", "todoist.create", "todoist.reopen", "todoist.update"])
        self.assertEqual(self.todoist.tasks["101"]["labels"], ["butler-waiting"])
        self.assertEqual(self.todoist.comments[0]["content"], "note")
        refused = self.library("ts.guarded(ts.delete, '101')")
        self.assertEqual(refused.returncode, 3, refused.stdout)
        self.assertIn("подтверждени", refused.stderr)
        self.assertIn("101", self.todoist.tasks)
        pending = [o for o in self.core.operations.list(limit=10) if o["kind"] == "todoist.delete"]
        self.assertEqual(pending[0]["state"], "awaiting_approval")
        self.core.approvals.grant(pending[0]["id"], pending[0]["review_code"], "terminal:owner", "terminal")
        self.core.wake()
        self.core.wait(pending[0]["id"], 20)
        self.assertNotIn("101", self.todoist.tasks)
        # Asking again for the same delete finds the finished operation instead of a second request.
        again = self.library("ts.guarded(ts.delete, '101')")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(len([o for o in self.core.operations.list(limit=10) if o["kind"] == "todoist.delete"]), 1)

    def test_lost_response_creates_no_second_task(self):
        self.todoist.fail = "timeout-after"
        done = self.library("print(ts.add_task('Один раз')['id'])")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "101")
        self.assertEqual(len(self.todoist.tasks), 1)
        self.assertEqual(self.core.operations.list(limit=1)[0]["reason"], "reconciled: effect found")

    def test_core_down_refuses_without_writing(self):
        self.server.stop()
        done = self.library("ts.guarded(ts.add_task, 'Никуда')", switched=True)
        self.assertEqual(done.returncode, 3, done.stdout)
        self.assertIn("недоступно", done.stderr)
        self.assertEqual(self.todoist.tasks, {})

    def test_switch_off_keeps_the_direct_path(self):
        done = self.library("print(ts.add_task('Напрямую')['id'])", switched=False)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(self.core.operations.list(limit=10), [])
        self.assertEqual(len(self.todoist.tasks), 1)
        self.assertEqual([r for r in self.todoist.requests if r[1] == "/sync"], [])


if __name__ == "__main__":
    unittest.main()
