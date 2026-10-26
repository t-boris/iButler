"""Email wave: scheduled readings, digest delivery, approved replies and Sent-mailbox reconciliation."""
import datetime as dt
import json
import os
import stat
import unittest
from zoneinfo import ZoneInfo

from butler_core.adapters.mail import MailAdapter
from butler_core.service import Core
from support import OWNER, CoreCase, base_config

ZONE = ZoneInfo("America/Chicago")

FAKE_CLI = r'''#!/usr/bin/env python3
"""A stand-in for bin/butler-mail driven by a mode file."""
import json, sys, time
from pathlib import Path
here = Path(__file__).resolve().parent.parent
mode = (here / "mode").read_text().strip() if (here / "mode").exists() else "ok"
args = sys.argv[1:]
(here / "calls.jsonl").open("a").write(json.dumps(args) + "\n")
def opt(name, default=None):
    return args[args.index(name) + 1] if name in args else default
if args[0] == "scan":
    if mode == "mail-closed":
        print("error: Mail.app unavailable: Mail.app is not running", file=sys.stderr); sys.exit(6)
    digest = "" if mode == "quiet" else "📬 Почта · утро: писем 12, важных 2\n1. Anna <anna@example.com> — Встреча во вторник (24.10 09:10, example.com)\n   Предлагает перенести встречу.\n   ↩ ждут ответа — скажи «ответь на письмо 1», и я подготовлю черновик"
    print(json.dumps({"reading": "rd-20261024T0830-ab12", "slot": opt("--slot"), "total": 12, "new": 12, "important": 0 if mode == "quiet" else 2,
                      "needs_reply": 1, "stale_hours": 0.4, "digest": digest, "offers": [{"ordinal": 1, "key": "k1"}]}))
    sys.exit(0)
if args[0] == "reply":
    if mode == "mail-closed":
        print("error: Mail.app unavailable", file=sys.stderr); sys.exit(6)
    if mode == "draft-changed":
        print("error: draft changed since approval; nothing sent — review and approve again", file=sys.stderr); sys.exit(1)
    (here / "sent.json").write_text(json.dumps({"key": args[1], "text": opt("--text"), "operation": opt("--core-operation"),
                                                "outgoing": opt("--outgoing"), "sha": opt("--sha")}))
    if mode == "die-after-send":
        (here / "started").write_text(str(time.time()))
        sys.exit(137)
    print(json.dumps({"key": args[1], "account": "t.boris@gmail.com", "subject": "Re: Встреча во вторник", "sent_at": time.time()}))
    sys.exit(0)
if args[0] == "find-sent":
    started = float((here / "started").read_text()) if (here / "started").exists() else None
    age = None if started is None else int((here / "age").read_text()) if (here / "age").exists() else (None if started is None else 1)
    found = {"message_id": "<sent-1@example.com>", "mailbox": "Sent"} if (here / "found").exists() else None
    print(json.dumps({"key": args[1], "started_at": started, "sent_at": None, "found": found, "age": age}))
    sys.exit(0)
sys.exit(2)
'''


def mail_config():
    config = base_config()
    config["operations"]["mail.scan"] = {"adapter": "mail", "approval": "none", "reconcile": "owner", "max_attempts": 1, "timeout": 900}
    config["operations"]["mail.reply"] = {"adapter": "mail", "approval": "required", "reconcile": "auto", "max_attempts": 2, "timeout": 240}
    config["personas"]["butler"]["actions"] += ["mail.scan", "mail.reply"]
    config["scenarios"]["mail"] = {"enabled": True}
    config["mail"] = {"times": ["08:30", "17:30"], "zone": "America/Chicago", "lookback_hours": 24}
    config["delivery"]["initiative"]["digest"] = {"per_day": 3, "interval": 0}
    config["limits"]["workers"]["mail"] = 1
    return config


def at(hour, minute=0, day=22):
    return dt.datetime(2026, 10, day, hour, minute, tzinfo=ZONE).timestamp()


class MailCase(CoreCase):
    def make_core(self, **kwargs):
        self.ws = self.tmp / "ws"
        (self.ws / "bin").mkdir(parents=True)
        cli = self.ws / "bin/butler-mail"
        cli.write_text(FAKE_CLI)
        cli.chmod(cli.stat().st_mode | stat.S_IXUSR)
        self.mail = MailAdapter(self.ws, os.environ.get("PATH", "/usr/bin:/bin"), {"TMPDIR": "/tmp"}, clock=self.clock)
        return Core(self.tmp / "state", adapters={"test": self.adapter, "mail": self.mail},
                    transports={"telegram:butler": self.transport}, clock=self.clock, reserve=0)

    def setUp(self):
        super().setUp()
        self.clock.now = at(8, 0)
        self.activate(mail_config())

    def mode(self, value):
        (self.ws / "mode").write_text(value)

    def calls(self):
        path = self.ws / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def run_pending(self):
        states = {}
        for op in self.core.operations.list(limit=20):
            if op["state"] == "requested":
                states[op["id"]] = self.core.operations.run(op["id"])
        return states


class ReadingTest(MailCase):
    def test_readings_are_scheduled_once_per_slot_and_deliver_the_digest(self):
        self.core.schedule_mail_readings()
        self.assertEqual(self.core.operations.list(limit=5), [])            # 08:00: nothing due yet
        self.clock.now = at(8, 31)
        self.core.schedule_mail_readings()
        self.core.schedule_mail_readings()                                  # the minute loop runs again: still one
        ops = self.core.operations.list(limit=5)
        self.assertEqual(len(ops), 1)
        self.assertEqual((ops[0]["kind"], ops[0]["state"]), ("mail.scan", "requested"))
        self.assertEqual(self.core.operations.run(ops[0]["id"]), "succeeded")
        self.assertEqual(self.calls()[0][:5], ["scan", "--json", "--slot", "morning", "--hours"])
        record = self.core.operations.get(ops[0]["id"])
        self.assertEqual(record["receipt"], "mail:reading:rd-20261024T0830-ab12")
        deliveries = self.core.delivery.list()
        self.assertEqual([(d["kind"], d["state"]) for d in deliveries], [("digest", "pending")])
        self.assertEqual(self.core.delivery.send(deliveries[0]["id"]), "delivered")
        self.assertIn("Почта · утро", self.transport.sent[0]["content"])
        self.assertNotIn("body", self.transport.sent[0]["content"])
        self.clock.now = at(17, 45)
        self.core.schedule_mail_readings()
        kinds = [(o["kind"], json_params(o, self.core)["slot"]) for o in self.core.operations.list(limit=5)]
        self.assertEqual(sorted(kinds), [("mail.scan", "evening"), ("mail.scan", "morning")])

    def test_quiet_reading_sends_nothing(self):
        self.mode("quiet")
        self.clock.now = at(8, 31)
        self.core.schedule_mail_readings()
        self.run_pending()
        self.assertEqual(self.core.delivery.list(), [])
        self.assertEqual(self.core.operations.list(limit=1)[0]["state"], "succeeded")

    def test_closed_mail_app_fails_visibly_and_the_next_slot_retries(self):
        self.mode("mail-closed")
        self.clock.now = at(8, 31)
        self.core.schedule_mail_readings()
        self.run_pending()
        op = self.core.operations.list(limit=1)[0]
        self.assertEqual(op["state"], "failed")
        self.assertIn("Mail.app", op["reason"])
        self.assertIn("mail:reading", [a["key"] for a in self.core.diagnostics.alarms()])
        self.mode("ok")
        self.clock.now = at(17, 31)
        self.core.schedule_mail_readings()
        self.run_pending()
        self.assertNotIn("mail:reading", [a["key"] for a in self.core.diagnostics.alarms()])

    def test_a_missed_slot_is_not_run_hours_late_and_the_switch_off_stops_scheduling(self):
        self.clock.now = at(15, 0)                                           # 08:30 is 6.5 h ago
        self.core.schedule_mail_readings()
        self.assertEqual(self.core.operations.list(limit=5), [])
        config = mail_config()
        config["scenarios"]["mail"] = {"enabled": False}
        self.activate(config)
        self.clock.now = at(17, 31)
        self.core.schedule_mail_readings()
        self.assertEqual(self.core.operations.list(limit=5), [])


class ReplyTest(MailCase):
    def submit_reply(self, key="k1"):
        return self.core.submit({"kind": "mail.reply", "persona": "butler", "principal": "agent:butler", "target": "mail:t.boris@gmail.com",
                                 "params": {"key": key, "text": "Хорошо, во вторник в 10.", "to": "anna@example.com",
                                            "subject": "Встреча во вторник"}, "idempotency_key": f"mail:reply:{key}"}, "system2")

    def test_reply_waits_for_the_owner_then_sends_once(self):
        op = self.submit_reply()
        self.assertEqual(op["state"], "awaiting_approval")
        approval = [d for d in self.core.delivery.list() if d["kind"] == "approval"]
        self.assertEqual(len(approval), 1)
        self.assertEqual(self.calls(), [])
        self.core.approvals.grant(op["id"], op["review_code"], "terminal:owner", "terminal")
        self.assertEqual(self.core.operations.run(op["id"]), "succeeded")
        sent = json.loads((self.ws / "sent.json").read_text())
        self.assertEqual((sent["key"], sent["operation"]), ("k1", op["id"]))
        self.assertEqual(self.core.operations.get(op["id"])["receipt"], "mail:sent:k1")
        again = self.submit_reply()
        self.assertTrue(again["duplicate"])

    def test_lost_send_is_settled_from_the_sent_mailbox(self):
        self.mode("die-after-send")
        op = self.submit_reply(key="k2")
        self.core.approvals.grant(op["id"], op["review_code"], "terminal:owner", "terminal")
        self.assertEqual(self.core.operations.run(op["id"]), "needs_reconciliation")   # just sent: Sent may lag
        self.assertEqual(len([c for c in self.calls() if c[0] == "reply"]), 1)
        (self.ws / "found").write_text("")
        self.assertEqual(self.core.operations.reconcile(op["id"]), "succeeded")
        self.assertEqual(len([c for c in self.calls() if c[0] == "reply"]), 1)

    def test_lost_send_absent_after_the_grace_is_retried(self):
        self.mode("die-after-send")
        op = self.submit_reply(key="k3")
        self.core.approvals.grant(op["id"], op["review_code"], "terminal:owner", "terminal")
        self.assertEqual(self.core.operations.run(op["id"]), "needs_reconciliation")
        (self.ws / "age").write_text("900")
        self.mode("ok")
        self.assertEqual(self.core.operations.reconcile(op["id"]), "requested")
        self.assertEqual(self.core.operations.run(op["id"]), "succeeded")

    def submit_mail_edited_reply(self, key="k5", sha="a" * 64):
        return self.core.submit({"kind": "mail.reply", "persona": "butler", "principal": "agent:butler", "target": "mail:t.boris@gmail.com",
                                 "params": {"key": key, "text": "Финальный текст из Mail.app", "to": ["anna@example.com"], "cc": [],
                                            "subject": "Re: Встреча во вторник", "account": "t.boris@gmail.com",
                                            "outgoing_id": 4242, "sha": sha}, "idempotency_key": f"mail:reply:{key}:{sha[:8]}"}, "system2")

    def test_mail_edited_reply_sends_the_bound_outgoing_message(self):
        op = self.submit_mail_edited_reply()
        self.assertEqual(op["state"], "awaiting_approval")
        self.core.approvals.grant(op["id"], op["review_code"], "terminal:owner", "terminal")
        self.assertEqual(self.core.operations.run(op["id"]), "succeeded")
        sent = json.loads((self.ws / "sent.json").read_text())
        self.assertEqual((sent["key"], sent["outgoing"], sent["sha"]), ("k5", "4242", "a" * 64))   # that message, that content
        edited = self.submit_mail_edited_reply(sha="b" * 64)                                        # an edit = a new approval
        self.assertFalse(edited.get("duplicate"))
        self.assertEqual(edited["state"], "awaiting_approval")

    def test_a_draft_changed_after_approval_is_refused_not_sent(self):
        self.mode("draft-changed")
        op = self.submit_mail_edited_reply(key="k6")
        self.core.approvals.grant(op["id"], op["review_code"], "terminal:owner", "terminal")
        self.assertEqual(self.core.operations.run(op["id"]), "failed")                 # rejected by the adapter: no retry
        self.assertIn("draft changed since approval", self.core.operations.get(op["id"])["reason"])
        self.assertFalse((self.ws / "sent.json").exists())

    def test_closed_mail_app_does_not_count_as_a_send(self):
        self.mode("mail-closed")
        op = self.submit_reply(key="k4")
        self.core.approvals.grant(op["id"], op["review_code"], "terminal:owner", "terminal")
        self.assertEqual(self.core.operations.run(op["id"]), "requested")           # not dispatched: retry later
        self.assertFalse((self.ws / "sent.json").exists())


def json_params(op, core):
    from butler_core.store import loads
    return loads(core.operations.get(op["id"])["params"], {})


if __name__ == "__main__":
    unittest.main()
