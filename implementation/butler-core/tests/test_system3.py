"""System 3 (REQ-027): signals become lesson candidates locally, the owner confirms, compliance is checked; never acts."""
import datetime as dt
import json
import os
import stat
import unittest
from zoneinfo import ZoneInfo

from butler_core.service import Core
from butler_core.system3 import System3
from support import OWNER, CoreCase, base_config

ZONE = ZoneInfo("America/Chicago")

FAKE_LESSON_CLI = r'''#!/usr/bin/env python3
import json, sys
from pathlib import Path
here = Path(__file__).resolve().parent.parent
store = here / "LESSONS.md"
args = sys.argv[1:]
(here / "calls.jsonl").open("a").write(json.dumps(args) + "\n")
if args[0] == "add":
    body = sys.stdin.read()
    with store.open("a") as f:
        f.write(f"\n## {args[1]} — {args[2]}\n\n{body}\n")
    sys.exit(0)
if args[0] == "retire":
    text = store.read_text() if store.exists() else ""
    blocks = text.split("\n## ")
    kept = [b for b in blocks if not b.startswith(args[1] + " ")]
    store.write_text("\n## ".join(kept))
    sys.exit(0)
sys.exit(2)
'''


def at(hour, minute=0, day=22):
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=ZONE).timestamp()


def system3_config():
    config = base_config()
    config["scenarios"]["system3"] = {"enabled": True}
    config["system3"] = {"questions_per_week": 2, "analysis_interval": 21600}
    config["delivery"]["initiative"]["question"] = {"per_day": 2, "interval": 0}
    return config


class System3Case(CoreCase):
    def setUp(self):
        super().setUp()
        self.clock.now = at(12)
        self.activate(system3_config())
        self.ws = self.tmp / "ws"
        (self.ws / "bin").mkdir(parents=True)
        cli = self.ws / "bin/butler-lesson"
        cli.write_text(FAKE_LESSON_CLI)
        cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
        self.verdicts = {"full_pause": {"cause": "too_intrusive", "confidence": 0.8, "lesson": "Не вмешивайся во время работы без обращения."},
                         "approval_rejected": {"cause": "unwanted_content", "confidence": 0.3, "lesson": "Не предлагать публикацию без запроса."}}
        self.prompts = []
        self.model_down = False

        def model(prompt):
            self.prompts.append(prompt)
            if self.model_down:
                raise ConnectionError("ollama down")
            kind = "full_pause" if "full_pause" in prompt else "approval_rejected"
            return dict(self.verdicts[kind])
        self.s3 = System3(self.core, model=model, workspace=self.ws, lesson_cli=cli, clock=self.clock)
        self.core.lessons = self.s3

    def signal(self, kind, persona="butler", **detail):
        return self.core.feedback.record(kind, persona=persona, principal="voice:owner", detail=detail)


class AnalysisTest(System3Case):
    def test_signals_become_candidate_lessons_with_provenance_and_metadata_only_prompts(self):
        self.core.delivery.enqueue(recipient=OWNER, channel="telegram:butler", persona="butler", kind="advice", content="совет", idempotency_key="a1")
        self.signal("full_pause", modality="video+audio", by="owner")
        self.signal("resume")
        self.clock.advance(3600)
        result = self.s3.analyse()
        self.assertEqual((result["analysed"], result["failed"]), (1, 0))        # the resume is evidence, not a signal
        lessons = self.s3.list()
        pause = [l for l in lessons if l["signal"] == "full_pause"][0]
        self.assertEqual((pause["cause"], pause["status"], pause["confidence"]), ("too_intrusive", "candidate", 0.8))
        self.assertEqual(len(pause["sources"]), 1)
        self.assertIn("full_pause", self.prompts[0])
        self.assertIn("advice", self.prompts[0])                     # what Butler had just done: kinds only
        self.assertNotIn("совет", self.prompts[0])                   # never the content
        self.assertEqual(self.core.feedback.list(unanalysed=True), [])
        summaries = [json.loads(l) for l in self.s3.summaries.read_text().splitlines()]
        self.assertEqual(summaries[0]["delivery_kinds"], ["advice"])
        # The same signal again merges into the open candidate instead of opening a second one.
        self.signal("full_pause", modality="video+audio")
        self.signal("resume")
        self.s3.analyse()
        pause = [l for l in self.s3.list() if l["signal"] == "full_pause"]
        self.assertEqual((len(pause), len(pause[0]["sources"])), (1, 2))

    def test_no_analysis_during_a_full_pause_and_model_outage_keeps_signals_for_later(self):
        self.signal("full_pause", modality="video+audio")
        self.assertEqual(self.s3.analyse()["skipped_full_pause"], 1)
        self.assertEqual(len(self.core.feedback.list(unanalysed=True)), 1)
        self.signal("resume")
        self.model_down = True
        result = self.s3.analyse()
        self.assertEqual(result["failed"], 1)
        self.assertIn("system3:model", [a["key"] for a in self.core.diagnostics.alarms()])
        self.model_down = False
        self.s3.analyse()
        self.assertNotIn("system3:model", [a["key"] for a in self.core.diagnostics.alarms()])
        self.assertEqual(self.core.feedback.list(unanalysed=True), [])

    def test_unclear_cause_asks_the_owner_at_most_twice_a_week_and_the_answer_is_evidence(self):
        for n in range(3):
            self.signal("approval_rejected")
            self.clock.advance(60)
        result = self.s3.analyse()
        self.assertEqual(result["asked"], 1)                         # one candidate, one question
        questions = [d for d in self.core.delivery.list() if d["kind"] == "question"]
        self.assertEqual(len(questions), 1)
        lesson = [l for l in self.s3.list() if l["signal"] == "approval_rejected"][0]
        self.assertIsNotNone(lesson["asked_at"])
        answered = self.s3.answer(lesson["id"][-6:], "ты предлагал публикацию, когда я просил только черновик — wrong_timing", "terminal:owner")
        self.assertEqual((answered["cause"], answered["confidence"]), ("wrong_timing", 0.8))
        self.assertIn("request", [f["kind"] for f in self.core.feedback.list()])

    def test_schedule_respects_the_switch_and_intervals(self):
        self.signal("full_pause")
        self.signal("resume")
        self.assertIn("analysis", self.s3.maybe_run())
        self.assertNotIn("analysis", self.s3.maybe_run() or {})
        config = system3_config()
        config["scenarios"]["system3"] = {"enabled": False}
        self.activate(config)
        self.assertIsNone(self.s3.maybe_run())


class LessonLifecycleTest(System3Case):
    def test_confirm_exports_to_the_persona_store_and_retire_removes_it(self):
        self.signal("full_pause")
        self.signal("resume")
        self.s3.analyse()
        lesson = [l for l in self.s3.list() if l["signal"] == "full_pause"][0]
        confirmed = self.s3.set_status(lesson["id"], "confirmed", "terminal:owner")
        self.assertEqual(confirmed["status"], "confirmed")
        self.assertTrue(confirmed["exported_id"].startswith("L-2026-10-22-"))
        store = (self.ws / "LESSONS.md").read_text()
        self.assertIn("[System 3]", store)
        self.assertIn("Не вмешивайся", store)
        corrected = self.s3.set_status(lesson["id"], "confirmed", "terminal:owner", text="Молчи, пока я не обращусь по имени.")
        self.assertEqual(corrected["text"], "Молчи, пока я не обращусь по имени.")
        retired = self.s3.set_status(lesson["id"], "retired", "terminal:owner")
        self.assertEqual(retired["status"], "retired")
        self.assertNotIn("[System 3]", (self.ws / "LESSONS.md").read_text())
        self.assertIsNone(self.s3.set_status(lesson["id"], "deleted", "terminal:owner"))
        self.assertEqual(self.s3.list(), [])

    def test_compliance_counts_repeats_and_alarms_after_three(self):
        self.signal("full_pause")
        self.signal("resume")
        self.s3.analyse()
        lesson = [l for l in self.s3.list() if l["signal"] == "full_pause"][0]
        self.s3.set_status(lesson["id"], "confirmed", "terminal:owner")
        self.clock.advance(10)
        self.assertEqual(self.s3.compliance()[0]["violations"], 0)
        for _ in range(3):
            self.clock.advance(600)
            self.signal("full_pause")
            self.signal("resume")
        report = self.s3.compliance()
        self.assertEqual(report[0]["violations"], 3)
        self.assertTrue(any(a["key"].startswith("system3:lesson:") for a in self.core.diagnostics.alarms()))
        notices = [d for d in self.core.delivery.list() if d["kind"] == "status"]
        self.assertEqual(len(notices), 1)
        self.assertEqual(self.core.operations.list(limit=5), [])      # System 3 never dispatches anything

    def test_doctor_snapshot_is_counts_only(self):
        snapshot = self.s3.snapshot()
        self.assertEqual(set(snapshot), {"enabled", "lessons", "unanalysed", "last_analysis", "last_check"})


if __name__ == "__main__":
    unittest.main()
