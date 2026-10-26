"""REQ-021/022/025/026/027: socket boundary, workers, crash recovery, backup, cron, transports, feedback."""
import json
import contextlib
import io
import os
import plistlib
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from butler_core.adapters.base import Ambiguous, NotDispatched, Rejected
from butler_core.adapters.openclaw import TelegramTransport, receipt_of
from butler_core.adapters.testing import ScriptedAdapter
from butler_core.backup import Backup, FileSet
from butler_core.client import Client, CoreError
from butler_core.cron import CronReader
from butler_core.daemon import CronJob, Transports, bootstrap
from butler_core.server import Server
from butler_core.service import Core
from support import OWNER, CoreCase, FileAdapter, base_config

ROOT = Path(__file__).resolve().parents[1]
TESTS = Path(__file__).resolve().parent


def short_dir():
    # AF_UNIX paths are limited to 104 bytes on macOS.
    return Path(tempfile.mkdtemp(prefix="bc-", dir="/tmp"))


class SocketCase(CoreCase):
    def setUp(self):
        super().setUp()
        self.caller = "local"
        self.sockdir = short_dir()
        self.server = Server(self.core, self.sockdir / "core.sock", classify=lambda pid: self.caller)
        self.server.start()
        self.client = Client(self.sockdir / "core.sock")

    def tearDown(self):
        self.server.stop()
        super().tearDown()
        import shutil
        shutil.rmtree(self.sockdir, ignore_errors=True)


class ServerBoundaryTest(SocketCase):
    def test_socket_is_private(self):
        self.assertEqual(os.stat(self.sockdir / "core.sock").st_mode & 0o777, 0o600)

    def test_local_callers_cannot_approve_or_activate(self):
        result = self.client.call("submit", request=self.request(kind="test.gated", key="g1"))
        self.assertEqual(result["state"], "awaiting_approval")
        for method, params in (("approve", {"operation": result["id"], "code": "000000"}),
                               ("reject", {"operation": result["id"]}),
                               ("resolve", {"operation": result["id"], "outcome": "done", "evidence": "x"}),
                               ("config.activate", {"version": 1}), ("config.rollback", {}),
                               ("bridge.admit", {"account": "butler"}), ("feedback.record", {"kind": "pause"})):
            with self.assertRaises(CoreError) as caught:
                self.client.call(method, **params)
            self.assertEqual(caught.exception.code, "forbidden", method)

    def test_terminal_owner_can_approve_and_rejection_is_feedback(self):
        result = self.client.call("submit", request=self.request(kind="test.gated", key="g2"))
        self.caller = "terminal"
        self.client.call("reject", operation=result["id"])
        self.assertEqual(self.core.operations.get(result["id"])["state"], "cancelled")
        kinds = [f["kind"] for f in self.core.feedback.list()]
        self.assertIn("approval_rejected", kinds)

    def test_gateway_admits_and_agents_cannot(self):
        self.caller = "gateway"
        admitted = self.client.call("bridge.admit", account="butler", sender="1000001", chat="1000001",
                                    message_id="77", text="привет")
        self.assertTrue(admitted["admitted"])
        self.caller = "local"
        with self.assertRaises(CoreError):
            self.client.call("bridge.admit", account="butler", sender="1000001", chat="1000001", message_id="78",
                             text="forged")

    def test_local_programs_notify_the_owner_through_delivery(self):
        board = self.sockdir / "tasks.html"
        board.write_text("<html/>")
        first = self.client.call("notify", text="📋 задачи", idempotency_key="tasks:1", kind="result",
                                 attachment={"path": str(board), "name": "tasks.html", "mime": "text/html"})
        self.assertEqual(first["state"], "pending")
        again = self.client.call("notify", text="📋 задачи", idempotency_key="tasks:1", kind="result")
        self.assertTrue(again["duplicate"])
        status = self.client.call("notify", text="starting", idempotency_key="status:1")
        self.assertEqual((status["kind"], status["state"]), ("status", "pending"))
        with self.assertRaises(CoreError) as caught:
            self.client.call("delivery.import", entries=[{"key": "x"}])
        self.assertEqual(caught.exception.code, "forbidden")
        self.caller = "terminal"
        self.assertEqual(self.client.call("delivery.import", entries=[{"key": "x", "state": "failed"}]),
                         {"imported": 1})
        states = {d["idempotency_key"]: d["state"] for d in self.client.call("delivery.list")}
        self.assertEqual(states["import:x"], "failed")

    def test_notify_command_queues_and_exits_3_when_the_core_is_away(self):
        import subprocess
        import sys
        command = [sys.executable, "-m", "butler_core.cli", "--socket", str(self.sockdir / "core.sock"), "--json",
                   "notify", "--key", "status:cli:1", "--text", "starting", "--wait", "0"]
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
        done = subprocess.run(command, capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(json.loads(done.stdout)["state"], "pending")
        self.assertEqual(self.core.delivery.list()[0]["kind"], "status")
        command[4] = str(self.sockdir / "absent.sock")
        gone = subprocess.run(command, capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(gone.returncode, 3, gone.stderr)
        self.assertIn("unavailable, retry", gone.stderr)
        self.assertEqual(len(self.core.delivery.list()), 1)

    def test_bridge_refresh_is_its_liveness_signal(self):
        self.caller = "gateway"
        self.client.call("bridge.hello", instance="gw-1", version="0.1.0", host="openclaw", started_at=self.clock())
        self.clock.advance(600)
        self.assertEqual(self.core.doctor()["bridge"]["seen_age_s"], 600)
        self.client.call("bridge.config")
        self.assertEqual(self.core.doctor()["bridge"]["seen_age_s"], 0)

    def test_ambient_reports_pause_as_metadata_only(self):
        self.caller = "ambient"
        self.client.call("feedback.record", kind="full_pause", detail={"modality": "video+audio",
                                                                       "transcript": "never stored"})
        record = self.core.feedback.list(kinds=["full_pause"])[0]
        self.assertEqual(record["principal"], "voice:owner")
        self.assertEqual(record["detail"], {"modality": "video+audio"})


class WorkerTest(CoreCase):
    def make_core(self, **kwargs):
        return Core(self.tmp / "state", adapters={"test": self.adapter}, transports={"telegram:butler":
                                                                                      self.transport},
                    reserve=0, interval=0.05, **kwargs)

    def test_stalled_executor_does_not_block_dialogue_or_other_work(self):
        self.adapter.script = ["stall"]
        self.core.start()
        stalled = self.core.submit(self.request(key="stall-1"), "cli")
        self.assertTrue(self.adapter.started.wait(5))
        began = time.monotonic()
        admitted = self.core.admit_message({"account": "butler", "sender": "1000001", "chat": "1000001",
                                            "message_id": "5", "text": "/tasks"})
        self.assertLess(time.monotonic() - began, 0.5)
        self.assertTrue(admitted["handled"])
        began = time.monotonic()
        other = self.core.submit(self.request(key="other-1"), "cli")
        row = self.core.wait(other["id"], 10)
        self.assertEqual(row["state"], "succeeded")
        self.assertLess(time.monotonic() - began, 60)
        self.assertEqual(self.core.operations.get(stalled["id"])["state"], "dispatching")
        self.core.operations.cancel(stalled["id"], "terminal")


CHILD = """
import sys, time
sys.path[:0] = [{root!r}, {tests!r}]
from support import FileAdapter, base_config
from butler_core.service import Core
core = Core({state!r}, adapters={{"test": FileAdapter({effects!r}, hang={hang!r})}}, reserve=0, interval=0.05)
version = core.config.propose(base_config(), "test")["version"]
core.config.activate(version, "test")
core.admit_message({{"account": "butler", "sender": "1000001", "chat": "1000001", "message_id": "900",
                    "text": "сделай это"}})
core.start()
core.submit({{"kind": "test.write", "persona": "butler", "principal": "agent:butler", "target": "x",
             "params": {{"v": 1}}, "idempotency_key": "crash-key"}}, "cli")
time.sleep(600)
"""


class CrashRecoveryTest(unittest.TestCase):
    """kill -9 in the middle of an external effect; a new process restores accounting without duplicates."""

    def crash(self, hang, marker):
        tmp = Path(tempfile.mkdtemp(prefix="butler-crash-"))
        state, effects = tmp / "state", tmp / "effects.txt"
        code = CHILD.format(root=str(ROOT), tests=str(TESTS), state=str(state), effects=str(effects), hang=hang)
        child = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
        self.addCleanup(child.stdout.close)
        try:
            deadline = time.monotonic() + 20
            line = ""
            while marker not in line and time.monotonic() < deadline:
                line = child.stdout.readline()
            self.assertIn(marker, line)
            os.kill(child.pid, signal.SIGKILL)
            child.wait(10)
        finally:
            if child.poll() is None:
                child.kill()
        adapter = FileAdapter(effects)
        began = time.monotonic()
        core = Core(state, adapters={"test": adapter}, reserve=0, interval=0.05)
        try:
            recovery = core.start()
            accounted = time.monotonic() - began
            op = core.store.one("SELECT id FROM operations WHERE idempotency_key='crash-key'")
            row = core.wait(op["id"], 30)
            deadline = time.monotonic() + 30
            while row["state"] not in ("succeeded", "failed", "needs_reconciliation") and time.monotonic() < deadline:
                time.sleep(0.05)
                row = core.operations.get(op["id"])
            chat = core.store.one("SELECT COUNT(*) FROM tasks WHERE kind='chat'")[0]
            return row, adapter.effects(), recovery, accounted, chat
        finally:
            core.stop()

    def test_kill_after_effect_reconciles_without_repeating(self):
        row, effects, recovery, accounted, chat = self.crash("after", "EFFECT")
        self.assertEqual(row["state"], "succeeded")
        self.assertEqual(effects, ["crash-key"])
        self.assertIn(row["id"], recovery["dispatching"])
        self.assertLess(accounted, 60)
        self.assertEqual(chat, 1)          # the admitted message survived the crash

    def test_kill_before_effect_retries_once(self):
        row, effects, recovery, accounted, _ = self.crash("before", "DISPATCHED")
        self.assertEqual(row["state"], "succeeded")
        self.assertEqual(effects, ["crash-key"])
        self.assertLess(accounted, 60)


class FakeVolume:
    def __init__(self, mount):
        self.mount, self.attached = Path(mount), 0

    def available(self):
        return True

    def attach(self, readonly=False):
        self.mount.mkdir(parents=True, exist_ok=True)
        self.attached += 1
        return self.mount

    def detach(self):
        pass


class BackupTest(CoreCase):
    def setUp(self):
        super().setUp()
        self.source = self.tmp / "workspace"
        (self.source / "state").mkdir(parents=True)
        (self.source / "state" / "notes.md").write_text("ordinary notes\n")
        (self.source / "auth-profiles.json").write_text('{"x": 1}')
        (self.source / "state" / "leak.txt").write_text("key sk-abcdefghijklmnopqrstuv here\n")
        (self.source / "clip.mp4").write_bytes(b"\0" * 10)
        (self.source / "config.json").write_text(json.dumps({"bot": {"botToken": "123456:" + "A" * 35, "n": 2}}))
        agents = self.tmp / "agents"
        agents.mkdir()
        with open(agents / "com.boris.test.plist", "wb") as stream:
            plistlib.dump({"EnvironmentVariables": {"API_KEY": "secret-value", "HOME": "/x"}}, stream)
        self.volume = FakeVolume(self.tmp / "mount")
        self.backup = Backup(self.core, volume=self.volume, keep=4, clock=self.clock,
                             sets=[FileSet("ws", self.source, redact=("config.json",)),
                                   FileSet("agents", agents, only=("com.boris.*.plist",), redact=("*.plist",))],
                             databases={"butler-core/core.sqlite3": self.core.store.path})

    def test_generation_has_manifest_and_no_secrets(self):
        self.core.admit_message({"account": "butler", "sender": "1000001", "chat": "1000001", "message_id": "1",
                                 "text": "hello"})
        result = self.backup.run()
        self.assertTrue(result["ok"], result)
        generation = self.tmp / "mount" / "generations" / result["generation"]
        manifest = json.loads((generation / "MANIFEST.json").read_text())
        self.assertIn("files/ws/state/notes.md", manifest["files"])
        self.assertIn("files/ws/auth-profiles.json", manifest["secrets_excluded"])
        self.assertIn("files/ws/state/leak.txt", manifest["secrets_excluded"])
        self.assertEqual(manifest["skipped"]["media"], 1)
        everything = b"".join(p.read_bytes() for p in generation.rglob("*") if p.is_file())
        self.assertNotIn(b"A" * 35, everything)
        self.assertNotIn(b"secret-value", everything)
        self.assertNotIn(b"sk-abcdefghijklmnopqrstuv", everything)
        config = json.loads((generation / "files/ws/config.json").read_text())
        self.assertEqual(config["bot"]["n"], 2)
        self.assertIn("secretRef", config["bot"]["botToken"])
        self.assertEqual(self.core.store.meta("backup_generation"), result["generation"])

    def test_unchanged_files_are_hard_links_and_generations_are_pruned(self):
        first = self.backup.run()
        self.clock.advance(6 * 3600)
        second = self.backup.run()
        a = self.tmp / "mount/generations" / first["generation"] / "files/ws/state/notes.md"
        b = self.tmp / "mount/generations" / second["generation"] / "files/ws/state/notes.md"
        self.assertEqual(a.stat().st_ino, b.stat().st_ino)
        self.assertGreaterEqual(second["linked"], 1)
        for _ in range(4):
            self.clock.advance(6 * 3600)
            self.backup.run()
        self.assertEqual(len(self.backup.generations(self.tmp / "mount")), 4)

    def test_staleness_alarm_and_restore_drill(self):
        self.core.admit_message({"account": "butler", "sender": "1000001", "chat": "1000001", "message_id": "2",
                                 "text": "remember this"})
        self.backup.run()
        self.clock.advance(13 * 3600)
        self.backup.interval = 10 ** 9
        self.backup.maybe_run()
        self.assertTrue(any(a["key"] == "backup:stale" for a in self.core.diagnostics.alarms()))
        target = self.tmp / "restored"
        drill = self.backup.restore(target, mount=self.tmp / "mount")
        self.assertEqual(drill["corrupt"], [])
        self.assertEqual(drill["accounting"]["tasks"].get("scheduled", 0) +
                         drill["accounting"]["tasks"].get("accepted", 0), 1)
        self.assertTrue((target / "files/ws/state/notes.md").is_file())

    def test_live_sqlite_in_a_file_set_is_copied_consistently(self):
        import sqlite3
        live = sqlite3.connect(self.source / "state" / "cache.db")
        live.execute("PRAGMA journal_mode=WAL")
        live.execute("PRAGMA wal_autocheckpoint=0")
        live.execute("CREATE TABLE t(v)")
        live.execute("INSERT INTO t VALUES('only in the wal')")
        live.commit()
        result = self.backup.run()
        live.close()
        copy = self.tmp / "mount/generations" / result["generation"] / "files/ws/state/cache.db"
        restored = sqlite3.connect(copy)
        self.assertEqual(restored.execute("SELECT v FROM t").fetchone()[0], "only in the wal")
        restored.close()
        manifest = json.loads((copy.parents[3] / "MANIFEST.json").read_text())
        self.assertNotIn("files/ws/state/cache.db-wal", manifest["files"])

    def test_corruption_is_detected_on_restore(self):
        result = self.backup.run()
        victim = self.tmp / "mount/generations" / result["generation"] / "files/ws/state/notes.md"
        os.unlink(victim)
        victim.write_text("tampered\n")
        drill = self.backup.restore(self.tmp / "restored2", mount=self.tmp / "mount")
        self.assertEqual(drill["corrupt"], ["files/ws/state/notes.md"])


class CronTest(CoreCase):
    def setUp(self):
        super().setUp()
        self.cron = self.tmp / "cron"
        (self.cron / "runs").mkdir(parents=True)
        (self.cron / "jobs.json").write_text(json.dumps({"jobs": [
            {"id": "job-a", "name": "news-digest-morning", "agentId": "butler", "enabled": True},
            {"id": "job-b", "name": "Memory Dreaming", "agentId": None, "enabled": True}]}))
        (self.cron / "jobs-state.json").write_text(json.dumps({"version": 1, "jobs": {}}))
        self.reader = CronReader(self.cron)
        self.job = CronJob(self.core, self.reader)

    def append(self, job, run_at, status):
        with open(self.cron / "runs" / f"{job}.jsonl", "a") as stream:
            stream.write(json.dumps({"ts": run_at + 5000, "jobId": job, "action": "finished", "status": status,
                                     "runAtMs": run_at, "durationMs": 5000,
                                     "sessionKey": f"agent:butler:cron:{job}"}) + "\n")

    def test_runs_become_tasks_once(self):
        now_ms = int(self.clock() * 1000)
        self.append("job-a", now_ms - 60_000, "ok")
        self.append("job-b", now_ms - 30_000, "error")
        self.assertEqual(self.job.reconcile()["recorded"], 2)
        self.assertEqual(self.job.reconcile()["recorded"], 0)
        self.reader.offsets.clear()                          # a re-read never duplicates
        self.assertEqual(self.job.reconcile()["recorded"], 0)
        tasks = {t["goal"]: t for t in self.core.tasks.list(kinds=("cron",))}
        self.assertEqual(tasks["news-digest-morning"]["state"], "succeeded")
        self.assertEqual(tasks["Memory Dreaming"]["state"], "failed")
        self.assertEqual(tasks["Memory Dreaming"]["persona"], "butler")

    def test_running_marker_is_completed_by_finished_record(self):
        started = int(self.clock() * 1000)
        (self.cron / "jobs-state.json").write_text(json.dumps({"version": 1, "jobs": {
            "job-a": {"state": {"runningAtMs": started}}}}))
        self.job.reconcile()
        task = self.core.tasks.list(kinds=("cron",))[0]
        self.assertEqual(task["state"], "running")
        (self.cron / "jobs-state.json").write_text(json.dumps({"version": 1, "jobs": {"job-a": {"state": {}}}}))
        self.append("job-a", started + 3, "ok")
        self.job.reconcile()
        self.assertEqual(self.core.tasks.get(task["id"])["state"], "succeeded")
        self.assertEqual(self.core.store.one("SELECT COUNT(*) FROM cron_runs")[0], 1)

    def test_cron_tasks_are_not_listed_in_chat(self):
        self.append("job-a", int(self.clock() * 1000), "ok")
        self.job.reconcile()
        reply = self.core.admit_message({"account": "butler", "sender": "1000001", "chat": "1000001",
                                         "message_id": "3", "text": "/tasks"})
        self.assertNotIn("news-digest", reply["text"])


class TransportTest(unittest.TestCase):
    def runner(self, code, stdout="", stderr=""):
        calls = []

        def run(command, **kwargs):
            calls.append(command)
            return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)
        return run, calls

    def message(self, **extra):
        return {"recipient": OWNER, "content": "текст", "silent": False, **extra}

    def test_receipt_from_gateway_json(self):
        run, calls = self.runner(0, json.dumps({"payload": {"messageId": "4242"}}))
        transport = TelegramTransport("butler", runner=run, instance=lambda: "1@t")
        self.assertEqual(transport.send(self.message(silent=True)), "4242")
        self.assertIn("--silent", calls[0])
        self.assertEqual(calls[0][calls[0].index("--target") + 1], OWNER.split(":", 1)[1])
        self.assertFalse(transport.deduplicates)

    def test_failure_classes(self):
        cases = ((1, "", "Error: connect ECONNREFUSED 127.0.0.1:18789", NotDispatched),
                 (1, "", "gateway closed before ready (1006): ", NotDispatched),
                 (1, "", "Bad Request: chat not found", Rejected),
                 (1, "", "gateway closed (1006): abnormal closure", Ambiguous),
                 (0, "Sent via telegram", "", Ambiguous))
        for code, out, err, expected in cases:
            run, _ = self.runner(code, out, err)
            with self.assertRaises(expected, msg=err or out):
                TelegramTransport("butler", runner=run, instance=lambda: None).send(self.message())

    def test_timeout_is_ambiguous(self):
        def run(command, **kwargs):
            raise subprocess.TimeoutExpired(command, 45)
        with self.assertRaises(Ambiguous):
            TelegramTransport("butler", runner=run, instance=lambda: None).send(self.message())

    def test_transports_only_for_telegram_accounts(self):
        transports = Transports(factory=lambda account: account)
        self.assertEqual(transports.get("telegram:tetyana-dyck"), "tetyana-dyck")
        self.assertIsNone(transports.get("whatsapp:x"))
        self.assertIsNone(transports.get("telegram:../etc"))
        self.assertEqual(receipt_of('{"message_id": 7}'), "7")


class BootstrapAndFeedbackTest(CoreCase):
    def test_bootstrap_activates_once(self):
        root = self.tmp / "fresh"
        core = Core(root, adapters={"test": ScriptedAdapter("test")}, reserve=0)
        (root / "bootstrap-config.json").write_text(json.dumps(base_config()))
        self.assertEqual(bootstrap(core, root), 1)
        (root / "bootstrap-config.json").write_text(json.dumps(base_config(limits={"queue": 5})))
        self.assertEqual(bootstrap(core, root), 1)
        self.assertEqual(len(core.config.versions()), 1)

    def test_offline_admin_activation_is_idempotent_and_refused_while_serving(self):
        from butler_core import cli
        root = short_dir()
        self.addCleanup(shutil.rmtree, root, True)
        config = root / "config.json"
        # The offline path builds the daemon's real adapters, so the configuration may name only those.
        config.write_text(json.dumps(base_config(operations={"openclaw.agent_turn": {"adapter": "openclaw"}})))

        def activate():
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(["admin", "activate", str(config), "--state", str(root)]), 0)
            return json.loads(out.getvalue())

        first, second = activate(), activate()
        self.assertEqual(first["activated"]["version"], 1)
        self.assertEqual(second, {"unchanged": 1})
        self.assertFalse(json.loads((root / "switches.json").read_text())["scenarios"].get("dispatch", False))
        core = Core(root, adapters={"test": ScriptedAdapter("test")}, reserve=0)
        server = Server(core, root / "core.sock", classify=lambda pid: "local")
        server.start()
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(cli.main(["admin", "activate", str(config), "--state", str(root)]), 3)
        finally:
            server.stop()

    def test_request_and_reply_to_initiative(self):
        self.clock.now = 1_790_020_000.0          # daytime in Chicago
        self.core.delivery.enqueue(recipient=OWNER, channel="telegram:butler", persona="butler", kind="advice",
                                   content="совет", idempotency_key="adv-1")
        for delivery_id in self.core.delivery.ready():
            self.assertEqual(self.core.delivery.send(delivery_id), "delivered")
        self.core.admit_message({"account": "butler", "sender": "1000001", "chat": "1000001", "message_id": "10",
                                 "text": "не надо таких советов"})
        self.clock.advance(3600)
        self.core.admit_message({"account": "butler", "sender": "1000001", "chat": "1000001", "message_id": "11",
                                 "text": "сделай отчёт"})
        kinds = [f["kind"] for f in reversed(self.core.feedback.list())]
        self.assertEqual(kinds, ["reply_to_initiative", "request"])
        first = self.core.feedback.list(kinds=["reply_to_initiative"])[0]
        self.assertEqual(first["context"]["deliveries"][0]["kind"], "advice")
        self.assertNotIn("совет", json.dumps(first, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
