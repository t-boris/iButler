"""BUG-018: a worker that finished but never exits is not a timeout.

Eight of eighteen video dispatches since 2026-11-02 were recorded as failed ("worker exited -1") although the Claude CLI had
written a successful final result minutes after the start; the CLI then kept running (a background task of its own) until the
monitor's one-hour timeout killed it. The monitor now ends a worker that wrote its final ``result`` and stayed silent, and takes
the outcome from that result. Runs the repository copy of the dispatcher (news-video/live-patches/lib/dispatch.py).
"""
import json
import os
import shutil
import subprocess
import time
import unittest
from pathlib import Path

from butler_core.adapters import butler as workspace_adapters
from test_dispatch import INSTALLED, PYTHON, fake_workspace, substitute
from test_runtime import short_dir

REPOSITORY_COPY = Path(__file__).resolve().parents[2] / "news-video/live-patches/lib/dispatch.py"
RESULT = '{"type":"result","subtype":"%s","is_error":%s,"total_cost_usd":%s,"usage":{"input_tokens":10,"output_tokens":5}}'


@unittest.skipUnless(REPOSITORY_COPY.is_file() and PYTHON.exists() and (INSTALLED / "lib/cost.py").is_file(),
                     "Butler workspace not installed")
class MonitorTest(unittest.TestCase):
    def setUp(self):
        self.root = short_dir()
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.ws = fake_workspace(self.root, dispatcher=REPOSITORY_COPY)
        source = (self.ws / "lib/dispatch.py").read_text()
        # Seconds instead of minutes, so the tests wait for the real mechanism and not for a clock.
        source = substitute(source, "RESULT_IDLE_GRACE = 300", "RESULT_IDLE_GRACE = 2")
        source = substitute(source, "MONITOR_POLL = 5", "MONITOR_POLL = 0.2")
        source = substitute(source, "TERMINATE_GRACE = 15", "TERMINATE_GRACE = 1.5")
        (self.ws / "lib/dispatch.py").write_text(source)

    def backend(self, script):
        path = self.ws / "fake-backend"
        path.write_text("#!/bin/sh\n" + script)
        path.chmod(0o755)

    def run_monitor(self, timeout=60, ident="20261110-050018-abc123"):
        directory = self.ws / "state/dispatches" / ident
        directory.mkdir(parents=True)
        (directory / "output.log").touch()
        (directory / "stderr.log").touch()
        (directory / "meta.json").write_text(json.dumps({
            "dispatch_id": ident, "task_kind": "review", "backend": "claude", "model": "sonnet",
            "project_path": str(self.ws / "projects/demo"), "prompt": "check", "timeout": timeout, "chain_depth": 1,
            "started_at": "2026-11-10T05:00:18", "started_at_ts": int(time.time()), "status": "running",
            "monitor_pid": None}))
        started = time.monotonic()
        subprocess.run([str(PYTHON), "-m", "lib.dispatch", "_monitor", ident], cwd=self.ws, timeout=120,
                       env={**os.environ, "PYTHONPATH": str(self.ws)}, capture_output=True)
        took = time.monotonic() - started
        result = json.loads((directory / "result.json").read_text())
        meta = json.loads((directory / "meta.json").read_text())
        return result, meta, (directory / "stderr.log").read_text(), (directory / "output.log").read_text(), took

    def test_a_worker_that_hangs_after_its_final_result_is_ended_and_counted_as_finished(self):
        self.backend(f"echo '{RESULT % ('success', 'false', '4.0')}'\nexec sleep 600\n")
        result, meta, stderr, _, took = self.run_monitor()
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(meta["status"], "done")
        self.assertLess(took, 30)                                       # not the 60 s timeout, let alone an hour
        self.assertIn("final result", stderr)
        self.assertNotIn("timeout", stderr)

    def test_the_cost_is_the_one_the_worker_reported(self):
        self.backend(f"echo '{RESULT % ('success', 'false', '4.0')}'\nexec sleep 600\n")
        result, *_ = self.run_monitor()
        self.assertEqual(result["estimated_cost_usd"], 4.0)             # the price table knows no Sonnet 5: it said 0.0
        self.assertEqual(result["input_tokens"], 10)

    def test_a_failed_final_result_that_hangs_is_a_failure(self):
        self.backend(f"echo '{RESULT % ('error_during_execution', 'true', '0.5')}'\nexec sleep 600\n")
        result, meta, stderr, *_ = self.run_monitor()
        self.assertEqual((result["exit_code"], meta["status"]), (1, "failed"))
        self.assertIn("final result", stderr)

    def test_a_worker_that_ignores_the_polite_stop_is_killed(self):
        self.backend(f"trap '' TERM\necho '{RESULT % ('success', 'false', '1.0')}'\nwhile true; do sleep 1; done\n")
        result, _, _, _, took = self.run_monitor()
        self.assertEqual(result["exit_code"], 0)
        self.assertLess(took, 30)

    def test_more_output_after_a_result_means_the_worker_is_still_working(self):
        # A new turn can follow a result (a background task finished). Silence is what ends a worker, not the result itself.
        self.backend(f"echo '{RESULT % ('success', 'false', '1.0')}'\nsleep 1\necho '{{\"type\":\"assistant\"}}'\nsleep 1\n"
                     f"echo '{RESULT % ('success', 'false', '2.0')}'\nexit 0\n")
        result, meta, stderr, output, _ = self.run_monitor()
        self.assertEqual((result["exit_code"], meta["status"]), (0, "done"))
        self.assertNotIn("final result", stderr)                        # it exited by itself
        self.assertEqual(output.count('"type":"result"'), 2)
        self.assertEqual(result["estimated_cost_usd"], 2.0)             # the last result counts

    def test_a_real_timeout_is_still_a_timeout(self):
        self.backend("echo '{\"type\":\"assistant\"}'\nexec sleep 600\n")
        result, meta, stderr, _, took = self.run_monitor(timeout=3)
        self.assertEqual((result["exit_code"], meta["status"]), (-1, "failed"))
        self.assertIn("timeout after 3s", stderr)
        self.assertLess(took, 30)

    def test_a_worker_that_exits_by_itself_keeps_its_own_exit_code(self):
        self.backend(f"echo '{RESULT % ('success', 'false', '1.0')}'\nexit 3\n")
        result, meta, stderr, *_ = self.run_monitor()
        self.assertEqual((result["exit_code"], meta["status"]), (3, "failed"))
        self.assertNotIn("final result", stderr)


if __name__ == "__main__":
    unittest.main()
