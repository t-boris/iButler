"""W2 compatibility: the installed Butler dispatcher and the ambient voice jobs as tracked core operations.

The harness copies the installed ``lib/dispatch.py`` into a temporary workspace
and points only its binaries and roots elsewhere: the coding backends become a
fake script, the gateway CLI becomes ``/usr/bin/true`` (no Telegram notice and
no agent wake can leave the test), and the project roots become a temporary
folder. Every other line of the dispatcher runs unchanged.
"""
import json
import os
import shutil
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

from butler_core.adapters.dispatcher import DispatchAdapter, VoiceTaskAdapter
from butler_core.adapters import butler as workspace_adapters
from butler_core.operations import _contract
from butler_core.server import Server
from butler_core.service import Core
from support import OWNER, CoreCase, base_config
from test_runtime import short_dir

INSTALLED = Path.home() / ".openclaw/workspace-butler"
PYTHON = workspace_adapters.WORKSPACE_PYTHON
AMBIENT_SOURCE = Path(__file__).resolve().parents[2] / "ambient-awareness"
AMBIENT_PYTHON = workspace_adapters.AMBIENT_PYTHON
CLIENT = Path(__file__).resolve().parents[1] / "butler_core/client.py"

FAKE_BACKEND = """#!/bin/sh
sleep "$(cat '{root}/fake-sleep' 2>/dev/null || echo 0)"
echo '{{"type":"result","result":"fake result"}}'
"""


def substitute(text, old, new):
    if text.count(old) != 1:
        raise AssertionError(f"installed dispatcher changed shape near {old!r}; update the harness")
    return text.replace(old, new)


def fake_workspace(root):
    ws = (root / "ws").resolve()
    (ws / "lib").mkdir(parents=True)
    (ws / "projects/demo").mkdir(parents=True)
    source = (INSTALLED / "lib/dispatch.py").read_text()
    source = substitute(source, 'WORKSPACE  = Path("/Users/boris/.openclaw/workspace-butler")', f'WORKSPACE  = Path("{ws}")')
    source = substitute(source, 'CLAUDE_BIN = "/Users/boris/.local/bin/claude"', f'CLAUDE_BIN = "{ws}/fake-backend"')
    source = substitute(source, 'OPENCLAW_BIN = "/opt/homebrew/bin/openclaw"', 'OPENCLAW_BIN = "/usr/bin/true"')
    source = substitute(source, "CODEX_BIN_CANDIDATES = [\n", f'CODEX_BIN_CANDIDATES = [\n    "{ws}/fake-backend",\n')
    source = substitute(source, "ALLOWED_PROJECT_ROOTS = [\n", f'ALLOWED_PROJECT_ROOTS = [\n    "{ws}/projects",\n')
    (ws / "lib/dispatch.py").write_text(source)
    cost = substitute((INSTALLED / "lib/cost.py").read_text(), 'WORKSPACE = Path("/Users/boris/.openclaw/workspace-butler")',
                      f'WORKSPACE = Path("{ws}")')
    (ws / "lib/cost.py").write_text(cost)
    (ws / "lib/__init__.py").write_text("")
    shutil.copy(CLIENT, ws / "lib/butler_core_client.py")
    backend = ws / "fake-backend"
    backend.write_text(FAKE_BACKEND.format(root=ws))
    backend.chmod(0o755)
    return ws


def dispatch_config(dispatch=False, voice=False):
    config = base_config()
    config["personas"]["butler"]["actions"] += ["dispatch.claude", "dispatch.codex", "voice.claude"]
    config["exports"] += [{"persona": "butler", "category": "task-prompt", "recipient": "anthropic"}]
    for kind, adapter, recipient in (("dispatch.claude", "dispatcher", "anthropic"), ("dispatch.codex", "dispatcher", "openai"),
                                     ("voice.claude", "voice-tasks", "anthropic")):
        config["operations"][kind] = {"adapter": adapter, "approval": "none", "max_attempts": 2, "timeout": 120,
                                      "export": [{"category": "task-prompt", "recipient": recipient}]}
    config["scenarios"].update({"dispatch": {"enabled": dispatch}, "voice_tasks": {"enabled": voice}})
    return config


class SourceVoiceAdapter(VoiceTaskAdapter):
    """The voice adapter running the ambient source checkout instead of the installed package."""

    def environment(self, operation):
        return super().environment(operation) | {"PYTHONPATH": str(AMBIENT_SOURCE)}


def wait_for(predicate, timeout=20.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError("condition not reached in time")


@unittest.skipUnless((INSTALLED / "lib/dispatch.py").is_file() and PYTHON.exists(), "Butler workspace not installed")
class DispatchCase(CoreCase):
    def setUp(self):
        self.root = short_dir()
        self.ws = fake_workspace(self.root)
        self.dispatcher = DispatchAdapter(self.ws, PYTHON, workspace_adapters.PATH)
        self.voice = SourceVoiceAdapter(self.ws, PYTHON, workspace_adapters.PATH, AMBIENT_PYTHON)
        super().setUp()
        self.activate(dispatch_config())

    def tearDown(self):
        for meta in (self.ws / "state/dispatches").glob("*/meta.json"):
            pid = json.loads(meta.read_text()).get("monitor_pid")
            if pid:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        super().tearDown()
        shutil.rmtree(self.root, ignore_errors=True)

    def make_core(self, **kwargs):
        return Core(self.tmp / "state", adapters={"test": self.adapter, "dispatcher": self.dispatcher,
                                                  "voice-tasks": self.voice},
                    transports={"telegram:butler": self.transport}, clock=self.clock, reserve=0, **kwargs)

    def sleep(self, seconds):
        (self.ws / "fake-sleep").write_text(str(seconds))

    def submit(self, key="20261024-100000-abc123", task_kind="review", project=None, backend="claude", **extra):
        params = {"dispatch_id": key, "task_kind": task_kind, "project": str(project or self.ws / "projects/demo"),
                  "prompt": "check the readme", "backend": backend, "model": "sonnet", "timeout": 60,
                  "chain_depth": 1} | extra
        result = self.core.submit({"kind": f"dispatch.{backend}", "persona": "butler", "principal": "agent:butler",
                                   "target": f"project:{params['project']}", "params": params,
                                   "idempotency_key": f"dispatch:{key}"}, "system2")
        self.assertEqual(result["state"], "requested", result)
        return result["id"]

    def legacy(self, *args):
        done = subprocess.run([str(PYTHON), "-m", "lib.dispatch", *args], cwd=self.ws, capture_output=True, text=True,
                              env={"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "PYTHONPATH": str(self.ws)},
                              timeout=60)
        return done

    def track_until(self, op_id, states=("succeeded", "failed", "cancelled")):
        return wait_for(lambda: self.core.operations.track(op_id) in states and self.core.operations.get(op_id))


class DispatchAdapterTest(DispatchCase):
    def test_runs_tracks_and_succeeds_with_the_installed_dispatcher(self):
        op = self.submit()
        self.assertEqual(self.core.operations.run(op), "running")
        row = self.track_until(op)
        self.assertEqual(row["state"], "succeeded")
        self.assertEqual(row["receipt"], "20261024-100000-abc123")
        self.assertEqual(json.loads(row["result"])["exit_code"], 0)
        status = json.loads(self.legacy("status", "20261024-100000-abc123", "--json").stdout)
        self.assertEqual(status["status"], "done")
        meta = json.loads((self.ws / "state/dispatches/20261024-100000-abc123/meta.json").read_text())
        self.assertEqual((meta["chain_depth"], meta["task_kind"]), (1, "review"))

    def test_lost_response_reconciles_to_the_existing_job_without_a_second_worker(self):
        self.sleep(5)
        op = self.submit()
        self.core.operations.run(op)
        with self.core.store.transaction(guard=False) as db:
            db.execute("UPDATE operations SET state='dispatching' WHERE id=?", (op,))
        self.assertEqual(self.core.operations.reconcile(op), "running")
        again = self.dispatcher.execute(_contract(self.core.operations.get(op)), None)
        self.assertEqual(again["receipt"], "20261024-100000-abc123")
        self.assertEqual(len(list((self.ws / "state/dispatches").iterdir())), 1)

    def test_killed_worker_is_interrupted_reported_and_not_restarted(self):
        self.sleep(30)
        op = self.submit()
        self.core.operations.run(op)
        meta_path = self.ws / "state/dispatches/20261024-100000-abc123/meta.json"
        pid = wait_for(lambda: json.loads(meta_path.read_text()).get("monitor_pid"))
        os.killpg(pid, signal.SIGKILL)
        wait_for(lambda: subprocess.run(["/bin/ps", "-p", str(pid)], capture_output=True).returncode != 0)
        row = self.track_until(op)
        self.assertEqual(row["state"], "failed")
        self.assertIn("interrupted", row["reason"])
        notice = self.core.store.one("SELECT kind, content FROM deliveries WHERE idempotency_key=?",
                                     (f"interrupted:{op}",))
        self.assertEqual(notice["kind"], "status")
        self.assertIn("прервана", notice["content"])
        self.assertNotIn("check the readme", notice["content"])
        status = json.loads(self.legacy("status", "20261024-100000-abc123", "--json").stdout)
        self.assertEqual(status["status"], "interrupted")
        listed = json.loads(self.legacy("list", "5", "--json").stdout)
        self.assertEqual(listed[0]["status"], "interrupted")
        self.assertEqual(self.core.operations.ready(float("inf")), [])

    def test_cancel_stops_the_worker_and_reports_partial_changes(self):
        self.sleep(30)
        op = self.submit()
        self.core.operations.run(op)
        meta_path = self.ws / "state/dispatches/20261024-100000-abc123/meta.json"
        pid = wait_for(lambda: json.loads(meta_path.read_text()).get("monitor_pid"))
        result = self.core.cancel(op, "terminal:owner")
        self.assertEqual((result["mode"], result["state"], result["effect"]), ("stop_executor", "cancelled", "partial"))
        self.assertNotEqual(subprocess.run(["/bin/ps", "-p", str(pid)], capture_output=True).returncode, 0)
        status = json.loads(self.legacy("status", "20261024-100000-abc123", "--json").stdout)
        self.assertEqual(status["status"], "cancelled")
        feedback = self.core.store.one("SELECT kind FROM feedback WHERE subject=?", (op,))
        self.assertEqual(feedback["kind"], "cancel")

    def test_workers_never_inherit_the_core_environment(self):
        os.environ["BUTLER_BOT_TOKEN"] = "must-not-leak"
        self.addCleanup(os.environ.pop, "BUTLER_BOT_TOKEN", None)
        op = self.submit()
        env = self.dispatcher.environment(_contract(self.core.operations.get(op)))
        self.assertNotIn("BUTLER_BOT_TOKEN", env)
        self.assertNotIn("must-not-leak", json.dumps(env))
        self.assertEqual(env["BUTLER_CHAIN_DEPTH"], "1")
        self.assertTrue(set(env) <= {"HOME", "USER", "LOGNAME", "LANG", "TMPDIR", "PATH", "PYTHONPATH",
                                     "BUTLER_CORE_OPERATION", "BUTLER_CHAIN_DEPTH", "NODE_EXTRA_CA_CERTS",
                                     "NODE_USE_SYSTEM_CA"})

    def test_identity_check_never_trusts_a_reused_pid(self):
        self.assertFalse(self.dispatcher.alive(os.getpid(), "20261024-100000-abc123"))

    def test_project_outside_the_roots_is_rejected_without_a_job(self):
        op = self.submit(project="/tmp")
        self.assertEqual(self.core.operations.run(op), "failed")
        row = self.core.operations.get(op)
        self.assertIn("rejected", row["reason"])
        self.assertNotIn("check the readme", row["reason"])
        self.assertFalse((self.ws / "state/dispatches/20261024-100000-abc123").exists())

    def test_invalid_dispatch_id_is_rejected(self):
        op = self.submit(key="../../etc")
        self.assertEqual(self.core.operations.run(op), "failed")


class DispatchCliTest(DispatchCase):
    def cli(self, *args, wait=None, socket=None, depth="0"):
        env = {"PATH": "/usr/bin:/bin", "HOME": str(Path.home()), "PYTHONPATH": str(self.ws),
               "BUTLER_CORE_STATE": str(self.tmp / "state"), "BUTLER_CORE_SOCKET": str(socket or self.sock),
               "BUTLER_CHAIN_DEPTH": depth}
        driver = ("import sys; from lib import butler_core_client as c, dispatch as d\n"
                  + (f"c.AVAILABILITY_WAIT = {wait}\n" if wait is not None else "")
                  + "sys.exit(d._main(sys.argv[1:]))")
        return subprocess.run([str(PYTHON), "-c", driver, *args], cwd=self.ws, env=env, capture_output=True, text=True,
                              timeout=120)

    def setUp(self):
        super().setUp()
        self.sockdir = short_dir()
        self.sock = self.sockdir / "core.sock"
        self.server = Server(self.core, self.sock, classify=lambda pid: "local")
        self.server.start()
        self.core.start()

    def tearDown(self):
        self.server.stop()
        shutil.rmtree(self.sockdir, ignore_errors=True)
        super().tearDown()

    def test_switch_on_records_the_dispatch_in_the_core_first(self):
        self.activate(dispatch_config(dispatch=True))
        done = self.cli("start", "review", "--project", str(self.ws / "projects/demo"), "--prompt", "hi", depth="2")
        self.assertEqual(done.returncode, 0, done.stderr)
        dispatch_id = done.stdout.strip()
        row = self.core.operations.by_key(f"dispatch:{dispatch_id}")
        self.assertEqual((row["kind"], row["principal"], row["source"]), ("dispatch.claude", "agent:butler", "system2"))
        self.assertEqual(json.loads(row["params"])["chain_depth"], 2)
        self.assertEqual(row["receipt"], dispatch_id)
        meta = json.loads((self.ws / "state/dispatches" / dispatch_id / "meta.json").read_text())
        self.assertEqual(meta["chain_depth"], 2)
        stopped = self.cli("kill", dispatch_id)
        self.assertEqual(stopped.returncode, 0, stopped.stderr)

    def test_switch_off_keeps_the_legacy_path(self):
        done = self.cli("start", "review", "--project", str(self.ws / "projects/demo"), "--prompt", "hi")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIsNone(self.core.operations.by_key(f"dispatch:{done.stdout.strip()}"))
        self.assertTrue((self.ws / "state/dispatches" / done.stdout.strip() / "meta.json").exists())

    def test_core_unavailable_refuses_without_starting_anything(self):
        self.activate(dispatch_config(dispatch=True))
        began = time.monotonic()
        done = self.cli("start", "review", "--project", str(self.ws / "projects/demo"), "--prompt", "hi", wait=1,
                        socket=self.sockdir / "missing.sock")
        self.assertEqual(done.returncode, 3)
        self.assertIn("unavailable", done.stderr)
        self.assertGreaterEqual(time.monotonic() - began, 1.0)
        self.assertFalse((self.ws / "state/dispatches").exists() and any((self.ws / "state/dispatches").iterdir()))

    def test_denied_dispatch_is_refused(self):
        config = dispatch_config(dispatch=True)
        config["personas"]["butler"]["actions"].remove("dispatch.claude")
        self.activate(config)
        done = self.cli("start", "review", "--project", str(self.ws / "projects/demo"), "--prompt", "hi")
        self.assertEqual(done.returncode, 3)
        self.assertIn("not granted", done.stderr)

    def test_switch_file_follows_activation_and_rollback(self):
        switches = json.loads((self.tmp / "state/switches.json").read_text())
        self.assertFalse(switches["scenarios"]["dispatch"])
        self.activate(dispatch_config(dispatch=True))
        self.assertTrue(json.loads((self.tmp / "state/switches.json").read_text())["scenarios"]["dispatch"])
        self.core.config.rollback("test")
        self.assertFalse(json.loads((self.tmp / "state/switches.json").read_text())["scenarios"]["dispatch"])


@unittest.skipUnless(AMBIENT_PYTHON.exists() and (AMBIENT_SOURCE / "butler_ambient/work_tasks.py").is_file(),
                     "ambient runtime not installed")
class VoiceTaskTest(DispatchCase):
    def reviewed(self):
        script = ("import json,sys; from butler_ambient.work_tasks import WorkTasks; "
                  "print(json.dumps(WorkTasks(None, sys.argv[1]).prepare({'project':'demo','task':'check the readme',"
                  "'kind':'review'})))")
        done = subprocess.run([str(AMBIENT_PYTHON), "-c", script, str(self.ws)], capture_output=True, text=True,
                              env={"PYTHONPATH": str(AMBIENT_SOURCE), "HOME": str(Path.home()), "PATH": "/usr/bin:/bin"},
                              timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_owner_confirmed_voice_job_runs_through_the_core(self):
        current = self.reviewed()
        key = "voice-20261024-100000-0123456789"
        params = {"voice_id": key, "project": current["project"], "task": current["task"], "task_kind": current["kind"],
                  "backend": current["route"]["backend"], "model": current["route"]["model"],
                  "timeout": current["timeout"]}
        op = self.core.submit({"kind": "voice.claude", "persona": "butler", "principal": "voice:owner",
                               "target": f"project:{current['project']}", "params": params,
                               "idempotency_key": f"voice:{key}"},
                              "ambient")["id"]
        self.assertEqual(self.core.operations.run(op), "running")
        launch = json.loads((self.ws / "state/dispatches" / key / "voice-launch.json").read_text())
        self.assertEqual(launch["operation"], op)
        row = self.track_until(op)
        self.assertEqual(row["state"], "succeeded", row["reason"])
        meta = json.loads((self.ws / "state/dispatches" / key / "meta.json").read_text())
        self.assertEqual(meta["channel"], "owner-local-voice")

    def test_changed_route_after_review_is_rejected(self):
        current = self.reviewed()
        key = "voice-20261024-100000-abcdefabcd"
        params = {"voice_id": key, "project": current["project"], "task": current["task"], "task_kind": current["kind"],
                  "backend": "claude", "model": "some-other-model", "timeout": current["timeout"]}
        op = self.core.submit({"kind": "voice.claude", "persona": "butler", "principal": "voice:owner",
                               "target": f"project:{current['project']}", "params": params,
                               "idempotency_key": f"voice:{key}"},
                              "ambient")["id"]
        self.assertEqual(self.core.operations.run(op), "failed")
        self.assertFalse((self.ws / "state/dispatches" / key).exists())


if __name__ == "__main__":
    unittest.main()
