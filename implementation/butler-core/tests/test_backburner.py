"""Back Burner wave: the nightly research cycle as one tracked, scheduled operation.

The cycle itself is the real ``lib.backburner`` from the repository, run by the real adapter in a temporary
workspace and vault (the Env of the workspace suite), so the contract between the two (the start command, the
identity marker in the process list, the result file) is exercised rather than assumed. Only the model worker is
never involved: these tests have no folder that is due.
"""
import datetime as dt
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

import deploy
from butler_core.adapters import butler as workspace_adapters
from butler_core.adapters.backburner import BackburnerAdapter
from butler_core.adapters.base import FAILED, RUNNING, SUCCEEDED, Rejected
from butler_core.config import ConfigError, validate_content
from butler_core.operations import OperationError, _contract
from butler_core.service import Core
from support import CoreCase, base_config
from test_backburner_workspace import Env
from test_dispatch import wait_for

ZONE = ZoneInfo("America/Chicago")
ID = "bb-20261028-003000-ab12cd"
OTHER = "bb-20261029-003000-ef34ab"


def at(hour, minute=0, day=28):
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=ZONE).timestamp()


def backburner_config(enabled=True, **rule):
    """The deploy wave applied to the test base, so a drift in the wave shows up here."""
    config = base_config()
    # The live configuration already grants these to the butler persona (initial_config); the test base does not.
    config["exports"] += [{"persona": "butler", "category": category, "recipient": recipient}
                          for category, recipient in (("task-prompt", "anthropic"), ("project-context", "anthropic"),
                                                      ("search-query", "web-search"))]
    deploy.wave_backburner(config)
    config["scenarios"]["backburner"] = {"enabled": enabled}
    config["backburner"].update(rule)
    return config


class BackburnerCase(CoreCase):
    def make_core(self, **kwargs):
        self.env = Env()
        self.addCleanup(self.env.close)
        self.ws = self.env.ws
        self.cycles = self.ws / "state/backburner/cycles"
        self.backburner = BackburnerAdapter(self.ws, sys.executable, os.environ.get("PATH", "/usr/bin:/bin"))
        return Core(self.tmp / "state", adapters={"test": self.adapter, "backburner": self.backburner},
                    transports={"telegram:butler": self.transport}, clock=self.clock, reserve=0)

    def setUp(self):
        super().setUp()
        self.clock.now = at(1, 0)
        self.activate(backburner_config())
        self.stand_ins = []

    def tearDown(self):
        for process in self.stand_ins:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            process.wait()
        for meta in self.cycles.glob("*/meta.json"):
            pid = json.loads(meta.read_text()).get("monitor_pid")
            if pid:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
        super().tearDown()

    def operations(self):
        return self.core.operations.list(limit=20)

    def submit(self, cycle_id=ID, key=None, **params):
        result = self.core.submit({"kind": "backburner.cycle", "persona": "butler", "principal": "system:schedule",
                                   "target": "backburner:all", "idempotency_key": key or f"backburner:cycle:{cycle_id}",
                                   "params": {"cycle_id": cycle_id} | params}, "core")
        self.assertEqual(result["state"], "requested", result)
        return result["id"]

    def stand_in(self, cycle_id=ID):
        """A long-running process shaped like a started cycle: its own group, the marker in its command line."""
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)", "-m", "lib.backburner", "cycle",
                                    "--id", cycle_id], start_new_session=True)
        self.stand_ins.append(process)
        (self.cycles / cycle_id).mkdir(parents=True, exist_ok=True)
        (self.cycles / cycle_id / "meta.json").write_text(json.dumps({"id": cycle_id, "monitor_pid": process.pid,
                                                                      "started_at_ts": int(time.time())}))
        return process

    def finish(self, cycle_id, result):
        (self.cycles / cycle_id).mkdir(parents=True, exist_ok=True)
        (self.cycles / cycle_id / "result.json").write_text(json.dumps(result))

    def track(self, op_id, states=("succeeded", "failed", "cancelled")):
        return wait_for(lambda: self.core.operations.track(op_id) in states and self.core.operations.get(op_id))

    def alarms(self):
        return {a["key"]: a["detail"] for a in self.core.diagnostics.alarms()}


class ScheduleTest(BackburnerCase):
    def tick(self, hour, minute=0, day=28):
        self.clock.now = at(hour, minute, day)
        self.core.schedule_backburner()

    def test_the_scheduler_runs_the_job_every_minute(self):
        self.core.start(workers=False)
        jobs = {job[2]: job for job in self.core.scheduler.periodic}
        self.assertEqual(jobs["backburner"][0], 60)
        self.assertEqual(jobs["backburner"][3], self.core.schedule_backburner)

    def test_nothing_is_submitted_while_the_switch_is_off(self):
        self.activate(backburner_config(enabled=False))
        self.tick(1)
        self.assertEqual(self.operations(), [])

    def test_the_cycle_is_submitted_once_a_day_with_the_owners_schedule(self):
        self.tick(0, 10)
        self.assertEqual(self.operations(), [])                                  # before the start time
        self.tick(0, 29)
        self.assertEqual(self.operations(), [])
        self.tick(0, 30)
        self.tick(0, 31)
        self.tick(3, 0)                                                          # the minute loop keeps ticking: still one
        ops = self.operations()
        self.assertEqual(len(ops), 1)
        op = self.core.operations.get(ops[0]["id"])
        self.assertEqual((op["kind"], op["state"], op["target"], op["principal"]),
                         ("backburner.cycle", "requested", "backburner:all", "system:schedule"))
        params = json.loads(op["params"])
        self.assertRegex(params.pop("cycle_id"), r"^bb-20261028-003000-[a-f0-9]{6}$")
        self.assertEqual(params, {"window_start": "00:30", "window_end": "06:00", "max_runs": 5})

    def test_nothing_starts_after_the_last_start_time_and_the_next_day_is_a_new_cycle(self):
        self.tick(5, 59)
        self.assertEqual(len(self.operations()), 1)
        self.tick(6, 0)
        self.tick(7, 30)
        self.assertEqual(len(self.operations()), 1)                              # the window of the 28th is over
        self.tick(0, 45, day=29)
        keys = sorted(json.loads(o["params"])["cycle_id"] for o in map(lambda o: self.core.operations.get(o["id"]), self.operations()))
        self.assertEqual(len(keys), 2)
        self.assertRegex(keys[1], r"^bb-20261029-004500-[a-f0-9]{6}$")

    def test_a_window_missed_entirely_is_not_made_up_later_in_the_day(self):
        self.tick(6, 0)                                                          # the last start time itself is already too late
        self.assertEqual(self.operations(), [])
        self.tick(8, 0)
        self.tick(14, 0)
        self.assertEqual(self.operations(), [])

    def test_the_schedule_comes_from_the_configuration_and_is_passed_on(self):
        self.activate(backburner_config(time="02:00", last_start="04:00", max_runs=3))
        self.tick(1, 30)
        self.assertEqual(self.operations(), [])
        self.tick(2, 15)
        params = json.loads(self.core.operations.get(self.operations()[0]["id"])["params"])
        self.assertEqual((params["window_start"], params["window_end"], params["max_runs"]), ("02:00", "04:00", 3))
        self.tick(4, 0, day=28)
        self.assertEqual(len(self.operations()), 1)

    def test_a_resubmission_of_the_same_night_is_a_duplicate_not_a_second_cycle(self):
        self.tick(1, 0)
        stored = self.core.operations.get(self.operations()[0]["id"])
        params = json.loads(stored["params"])
        self.assertEqual(params["cycle_id"][:19], "bb-20261028-010000-")
        request = {"kind": "backburner.cycle", "persona": "butler", "principal": "system:schedule", "target": "backburner:all",
                   "idempotency_key": "backburner:cycle:2026-10-28", "params": params}
        self.assertTrue(self.core.submit(request, "core").get("duplicate"))
        self.assertEqual(len(self.operations()), 1)


class ConfigTest(unittest.TestCase):
    ADAPTERS = {"test", "backburner"}

    def check(self, **rule):
        return validate_content(backburner_config(**rule), self.ADAPTERS)

    def test_the_deploy_wave_is_valid_and_leaves_the_night_job_off(self):
        self.check()
        wave = base_config()
        deploy.wave_backburner(wave)
        validate_content(wave, self.ADAPTERS)
        self.assertEqual(wave["scenarios"]["backburner"], {"enabled": False})
        rule = wave["operations"]["backburner.cycle"]
        self.assertEqual((rule["adapter"], rule["approval"], rule["max_attempts"]), ("backburner", "none", 1))
        self.assertGreaterEqual(rule["timeout"], 5 * 2700)                      # five runs of the per-run timeout
        self.assertIn("backburner.cycle", wave["personas"]["butler"]["actions"])
        self.assertEqual(wave["delivery"]["initiative"]["research"], {"per_day": 5, "interval": 0})
        self.assertEqual(wave["limits"]["workers"]["backburner"], 1)
        self.assertEqual(wave["backburner"], {"time": "00:30", "last_start": "06:00", "zone": "America/Chicago", "max_runs": 5})
        self.assertIn(deploy.wave_backburner, deploy.WAVES)

    def test_the_cycle_exports_are_named_and_match_the_dispatch_exports(self):
        wave = base_config()
        deploy.wave_backburner(wave)
        self.assertEqual({(e["category"], e["recipient"]) for e in wave["operations"]["backburner.cycle"]["export"]},
                         {("task-prompt", "anthropic"), ("project-context", "anthropic"), ("search-query", "web-search")})

    def test_unusable_values_are_refused(self):
        for bad in (dict(time="24:00"), dict(time="0:30"), dict(time=30), dict(last_start="6"), dict(last_start="06:60"),
                    dict(time="06:00", last_start="06:00"), dict(time="07:00"), dict(max_runs=0), dict(max_runs=21),
                    dict(max_runs=True), dict(max_runs="5"), dict(zone="Mars/Base"), dict(zone=5)):
            with self.assertRaises(ConfigError, msg=bad):
                self.check(**bad)
        content = backburner_config()
        content["backburner"] = ["time"]
        with self.assertRaises(ConfigError):
            validate_content(content, self.ADAPTERS)

    def test_a_missing_section_uses_the_defaults(self):
        content = backburner_config()
        del content["backburner"]
        validate_content(content, self.ADAPTERS)


class AdapterUnitTest(BackburnerCase):
    def operation(self, **params):
        return {"id": "op-1", "params": {"cycle_id": ID} | params}

    def test_the_start_command_carries_the_cycle_and_the_schedule(self):
        command, stdin = self.backburner.command(self.operation(only="Sirius Art", window_start="01:00", window_end="05:00", max_runs=3))
        self.assertEqual(command, [sys.executable, "-m", "lib.backburner", "start-cycle", "--id", ID, "--only", "Sirius Art",
                                   "--window-start", "01:00", "--window-end", "05:00", "--max-runs", "3"])
        self.assertIsNone(stdin)
        self.assertEqual(self.backburner.command(self.operation())[0][-2:], ["--id", ID])

    def test_a_folder_name_is_checked_before_anything_starts(self):
        for bad in ("", "x" * 81, "line\nbreak", "tab\t", 5, ["a"]):
            with self.assertRaises(Rejected, msg=bad):
                self.backburner.command(self.operation(only=bad))

    def test_the_cycle_id_must_have_the_expected_shape(self):
        for bad in ("", "../x", "bb-1", ID.upper(), "dispatch-20261028-003000-ab12cd", ID + "x", None, 7):
            with self.assertRaises(Rejected, msg=bad):
                self.backburner.key({"params": {"cycle_id": bad}})

    def test_the_marker_is_what_the_cycle_command_line_contains(self):
        self.assertEqual(self.backburner.marker(ID), f"-m lib.backburner cycle --id {ID}")
        self.assertEqual(self.backburner.directory(self.operation()), self.ws / "state/backburner/cycles" / ID)

    def test_a_symlinked_cycle_directory_is_refused(self):
        self.cycles.mkdir(parents=True)
        (self.cycles / ID).symlink_to(self.tmp)
        with self.assertRaises(Rejected):
            self.backburner.directory(self.operation())

    def test_a_finished_cycle_is_summarised_without_folder_contents(self):
        self.finish(ID, {"id": ID, "runs": [{"name": "A", "status": "applied", "reason": ""}, {"name": "B", "status": "failed", "reason": "x" * 500}],
                         "skipped": [{"name": "C", "reason": "paused"}], "failed_folders": ["B"] + ["y" * 200]})
        state, summary = self.backburner.track(self.operation())
        self.assertEqual(state, SUCCEEDED)
        self.assertEqual({k: v for k, v in summary.items() if k != "failed_folders"},
                         {"cycle_id": ID, "runs": 2, "skipped": 1, "failed_runs": 1})
        self.assertEqual(summary["failed_folders"], ["B", "y" * 80])
        self.assertNotIn("x" * 500, json.dumps(summary))
        self.finish(OTHER, {"runs": [], "skipped": [], "failed_folders": [f"F{i}" for i in range(30)]})
        self.assertEqual(self.backburner.track(self.operation(cycle_id=OTHER))[1]["failed_folders"], [f"F{i}" for i in range(20)])

    def test_a_cycle_stopped_on_request_is_a_failure_with_that_reason(self):
        self.finish(ID, {"exit_code": -15, "cancelled_by": "butler-core"})
        state, summary = self.backburner.track(self.operation())
        self.assertEqual((state, summary["reason"]), (FAILED, "stopped on request"))

    def test_a_cycle_that_just_started_is_running_even_before_its_pid_is_recorded(self):
        (self.cycles / ID).mkdir(parents=True)
        (self.cycles / ID / "meta.json").write_text(json.dumps({"id": ID, "monitor_pid": None, "started_at_ts": int(time.time())}))
        self.assertEqual(self.backburner.track(self.operation())[0], RUNNING)

    def test_a_cycle_that_never_recorded_a_process_is_lost_after_the_grace_time(self):
        (self.cycles / ID).mkdir(parents=True)
        (self.cycles / ID / "meta.json").write_text(json.dumps({"id": ID, "monitor_pid": None, "started_at_ts": int(time.time()) - 1000}))
        state, summary = self.backburner.track(self.operation())
        self.assertEqual((state, summary["interrupted"]), (FAILED, True))

    def test_a_live_cycle_is_running_and_a_reused_process_id_is_not_trusted(self):
        process = self.stand_in()
        self.assertEqual(self.backburner.track(self.operation())[0], RUNNING)
        self.assertTrue(self.backburner.alive(process.pid, ID))
        self.assertFalse(self.backburner.alive(process.pid, OTHER))              # same pid, another job's marker
        self.assertFalse(self.backburner.alive(os.getpid(), ID))                 # a live process that is not this job
        (self.cycles / OTHER).mkdir()
        (self.cycles / OTHER / "meta.json").write_text(json.dumps({"id": OTHER, "monitor_pid": process.pid, "started_at_ts": 1}))
        state, summary = self.backburner.track(self.operation(cycle_id=OTHER))
        self.assertEqual((state, summary["interrupted"]), (FAILED, True))

    def test_health_reports_the_harness(self):
        self.assertEqual(self.backburner.health()["ok"], True)
        (self.ws / "lib/backburner.py").unlink()
        self.assertEqual(self.backburner.health()["ok"], False)


class RegistrationTest(unittest.TestCase):
    def test_the_adapter_is_offered_only_when_the_harness_is_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp) / "ws"
            (ws / "lib").mkdir(parents=True)
            (ws / "lib/dispatch.py").write_text("")
            with mock.patch.object(workspace_adapters, "WORKSPACE", ws), \
                    mock.patch.object(workspace_adapters, "TETYANA_WORKSPACE", Path(tmp) / "none"):
                self.assertNotIn("backburner", workspace_adapters.adapters())
                (ws / "lib/backburner.py").write_text("")
                found = workspace_adapters.adapters()
            self.assertIsInstance(found["backburner"], BackburnerAdapter)
            self.assertEqual((found["backburner"].workspace, found["backburner"].python),
                             (ws.resolve(), str(workspace_adapters.WORKSPACE_PYTHON)))


class CycleThroughTheCoreTest(BackburnerCase):
    def test_a_night_cycle_runs_to_a_result_through_the_real_harness(self):
        self.env.folder("Paused", brief="status: paused\n")
        self.core.schedule_backburner()
        op_id = self.operations()[0]["id"]
        cycle_id = json.loads(self.core.operations.get(op_id)["params"])["cycle_id"]
        self.assertEqual(self.core.operations.run(op_id), "running")
        row = self.track(op_id)
        self.assertEqual(row["state"], "succeeded")
        self.assertEqual(row["receipt"], cycle_id)
        result = json.loads(row["result"])
        self.assertEqual((result["runs"], result["skipped"], result["failed_folders"]), (0, 1, []))
        meta = json.loads((self.cycles / cycle_id / "meta.json").read_text())
        self.assertIsInstance(meta["monitor_pid"], int)
        # The owner's schedule reached the harness: the panel shows the window the cycle really used.
        saved = json.loads((self.ws / "state/backburner/schedule.json").read_text())
        self.assertEqual({k: saved[k] for k in ("window_start", "window_end", "max_runs")},
                         {"window_start": "00:30", "window_end": "06:00", "max_runs": 5})
        self.assertEqual(self.alarms(), {})

    def test_a_manual_run_names_a_folder_that_must_exist_and_nothing_starts_otherwise(self):
        self.env.folder("Alpha", brief="status: paused\n")
        op_id = self.submit(OTHER, only="Nope", window_start="00:30", window_end="06:00", max_runs=5)
        self.assertEqual(self.core.operations.run(op_id), "failed")
        self.assertIn("no unique folder matches", self.core.operations.get(op_id)["reason"])
        self.assertFalse((self.cycles / OTHER).exists())                        # refused before a job was recorded

    def test_a_panel_press_is_accepted_for_any_folder_name_because_the_target_is_an_id(self):
        """The panel's «Запустить» submits as the owner with a hashed target; the real core refuses a target with a space or Cyrillic."""
        def press(target, only, key):
            return self.core.submit({"kind": "backburner.cycle", "persona": "butler", "principal": "voice:owner", "target": target,
                                     "idempotency_key": key, "params": {"cycle_id": OTHER, "only": only}}, "ambient")
        accepted = press("backburner:0dc75b35aafe", "Дата-центр Проект", f"backburner:manual:{OTHER}")
        self.assertEqual(accepted["state"], "requested", accepted)
        for target in ("backburner:Sirius Art", "backburner:Дата-центр"):
            with self.assertRaises(OperationError, msg=target):
                press(target, "Sirius Art", f"backburner:manual:{OTHER}-2")

    def test_a_retry_after_a_lost_response_finds_the_job_and_starts_nothing(self):
        self.stand_in()
        op_id = self.submit()
        with self.core.store.transaction(guard=False) as db:
            db.execute("UPDATE operations SET state='dispatching' WHERE id=?", (op_id,))
        self.assertEqual(self.core.operations.reconcile(op_id), "running")
        again = self.backburner.execute(_contract(self.core.operations.get(op_id)), None)
        self.assertEqual(again["receipt"], ID)
        self.assertEqual([p.name for p in self.cycles.iterdir()], [ID])

    def test_a_cycle_killed_without_a_result_is_interrupted_reported_and_not_restarted(self):
        process = self.stand_in()
        op_id = self.submit()
        self.assertEqual(self.core.operations.run(op_id), "running")
        os.killpg(process.pid, signal.SIGKILL)
        process.wait()
        row = self.track(op_id)
        self.assertEqual(row["state"], "failed")
        self.assertIn("interrupted", row["reason"])
        notice = self.core.store.one("SELECT kind, content FROM deliveries WHERE idempotency_key=?", (f"interrupted:{op_id}",))
        self.assertEqual(notice["kind"], "status")
        self.assertIn("backburner.cycle", notice["content"])
        self.assertIn("back burner cycle failed", self.alarms()["backburner:cycle"])
        self.assertEqual(self.core.operations.ready(float("inf")), [])

    def test_cancel_stops_the_cycle_and_its_children_and_records_the_stop(self):
        process = self.stand_in()
        op_id = self.submit()
        self.core.operations.run(op_id)
        result = self.core.cancel(op_id, "terminal:owner")
        self.assertEqual((result["mode"], result["state"], result["effect"]), ("stop_executor", "cancelled", "partial"))
        self.assertIsNotNone(process.wait(timeout=10))
        self.assertEqual(json.loads((self.cycles / ID / "result.json").read_text())["exit_code"], -15)


class AlarmTest(BackburnerCase):
    def run_cycle(self, cycle_id, result):
        self.finish(cycle_id, result)
        (self.cycles / cycle_id / "meta.json").write_text(json.dumps({"id": cycle_id, "monitor_pid": None, "started_at_ts": int(time.time())}))
        op_id = self.submit(cycle_id)
        self.core.operations.run(op_id)
        return self.track(op_id)

    def test_folders_that_keep_failing_raise_an_alarm_that_clears_when_they_recover(self):
        row = self.run_cycle(ID, {"id": ID, "runs": [{"name": "Alpha", "status": "failed"}], "skipped": [], "failed_folders": ["Alpha", "Beta"]})
        self.assertEqual(row["state"], "succeeded")
        self.assertEqual(self.alarms()["backburner:folders"], "back burner folders failing 3 nights in a row: Alpha, Beta")
        self.assertNotIn("backburner:cycle", self.alarms())
        self.run_cycle(OTHER, {"id": OTHER, "runs": [{"name": "Alpha", "status": "applied"}], "skipped": [], "failed_folders": []})
        self.assertEqual(self.alarms(), {})

    def test_a_failed_cycle_raises_an_alarm_and_the_next_good_one_clears_it(self):
        row = self.run_cycle(ID, {"exit_code": -15, "cancelled_by": "butler-core"})
        self.assertEqual(row["state"], "failed")
        self.assertEqual(self.alarms()["backburner:cycle"], "back burner cycle failed: stopped on request")
        self.run_cycle(OTHER, {"id": OTHER, "runs": [], "skipped": [], "failed_folders": []})
        self.assertEqual(self.alarms(), {})

    def test_other_operations_never_touch_the_back_burner_alarms(self):
        self.core.diagnostics.raise_alarm("backburner:cycle", "kept")
        self.core.diagnostics.raise_alarm("backburner:folders", "kept")
        op = self.core.submit(self.request(), "core")
        self.core.operations.run(op["id"])
        self.assertEqual(set(self.alarms()), {"backburner:cycle", "backburner:folders"})


class NoteDeliveryTest(BackburnerCase):
    """The harness sends its note as kind `research`: held through quiet hours, at most five a day."""

    def note(self, key):
        return self.core.notify("butler", f"🔥 Дата-центр · день 3 ({key})", "research", key)

    def test_a_note_written_overnight_is_held_until_the_morning(self):
        self.clock.now = at(2, 0)
        self.note("backburner:a:1")
        delivery = self.core.delivery.list()[0]
        self.assertEqual(self.core.delivery.send(delivery["id"]), "deferred")
        self.assertEqual(self.transport.sent, [])
        self.clock.now = at(8, 5)
        self.assertEqual(self.core.delivery.send(delivery["id"]), "delivered")
        self.assertIn("день 3", self.transport.sent[0]["content"])

    def test_the_same_note_never_goes_out_twice_and_the_daily_cap_holds(self):
        self.clock.now = at(9, 0)
        self.note("backburner:a:1")
        self.note("backburner:a:1")
        self.assertEqual(len(self.core.delivery.list()), 1)
        for number in range(2, 8):
            self.note(f"backburner:a:{number}")
        states = [self.core.delivery.send(d["id"]) for d in sorted(self.core.delivery.list(), key=lambda d: d["id"])]
        self.assertEqual((states.count("delivered"), states.count("cancelled")), (5, 2))


if __name__ == "__main__":
    unittest.main()
