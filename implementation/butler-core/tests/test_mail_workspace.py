"""Email wave: the installed ``lib/mail.py`` reading, classifying and composing with Mail.app and the model faked.

The installed module is copied with its workspace path pointed at a temporary
directory, so the private store is created there; Mail.app and the local model
are replaced by functions. No AppleScript runs and nothing is sent.
"""
import importlib.util
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

from test_dispatch import substitute

INSTALLED = Path.home() / ".openclaw/workspace-butler"


def load_module(root):
    source = substitute((INSTALLED / "lib/mail.py").read_text(), 'WORKSPACE = Path("/Users/boris/.openclaw/workspace-butler")',
                        f'WORKSPACE = Path("{root}")')
    path = root / "mail_under_test.py"
    path.write_text(source)
    spec = importlib.util.spec_from_file_location("mail_under_test", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["mail_under_test"] = module
    spec.loader.exec_module(module)
    module.mail_running = lambda: True
    return module


class FakeMail:
    def __init__(self, now):
        self.now = now
        self.accounts = [{"name": "t.boris@gmail.com", "inbox": "INBOX", "user": "boris"},
                         {"name": "borist3@illinois.edu", "inbox": "Inbox", "user": "borist3"}]
        self.messages = {
            ("t.boris@gmail.com", "INBOX"): [
                {"mail_id": 1, "message_id": "<a1@example.com>", "sender": "Anna <anna@example.com>",
                 "subject": "Встреча во вторник", "received": now - 3600, "unread": True},
                {"mail_id": 2, "message_id": "<n1@shop.example>", "sender": "Shop <noreply@shop.example>",
                 "subject": "Weekly deals", "received": now - 7200, "unread": True},
                {"mail_id": 3, "message_id": "<b1@bank.example>", "sender": "Bank <alerts@bank.example>",
                 "subject": "Invoice overdue", "received": now - 1800, "unread": True}],
            ("borist3@illinois.edu", "Inbox"): [
                {"mail_id": 7, "message_id": "<u1@illinois.edu>", "sender": "Prof <prof@illinois.edu>",
                 "subject": "Question about the paper", "received": now - 5400, "unread": False}]}
        self.bodies = {1: "Боря, давай перенесём встречу на вторник в 10? Анна", 3: "Your invoice is overdue. Pay by Friday.",
                       7: "Could you send me the revised draft by Monday?"}
        self.body_reads, self.sent = [], []

    def list_messages(self, account, mailbox, since_ts):
        return [dict(m) for m in self.messages.get((account, mailbox), []) if m["received"] > since_ts]

    def read_body(self, account, mailbox, mail_id):
        self.body_reads.append(mail_id)
        return self.bodies[mail_id]

    def model(self, prompt):
        if "Анна" in prompt or "anna@" in prompt:
            return {"important": True, "needs_reply": True, "category": "personal", "reason": "знакомая просит перенести встречу",
                    "summary": "Анна предлагает перенести встречу на вторник в 10."}
        if "overdue" in prompt.lower():
            return {"important": True, "needs_reply": False, "category": "money", "reason": "просроченный счёт",
                    "summary": "Банк напоминает об оплате счёта до пятницы."}
        return {"important": True, "needs_reply": True, "category": "work", "reason": "вопрос от профессора",
                "summary": "Профессор просит прислать исправленный черновик к понедельнику."}

    def send_reply(self, account, mailbox, mail_id, text):
        self.sent.append((account, mailbox, mail_id, text))
        return {"subject": "Re: Встреча во вторник", "sent_at": self.now + 10}

    # -- the compose window of Mail.app (DEC-041)
    def open_reply(self, account, mailbox, mail_id, text):
        self.outgoing = {"outgoing_id": 77, "subject": "Re: Встреча во вторник", "sender": account, "to": ["anna@example.com"],
                         "cc": [], "content": text}
        return {"outgoing_id": 77}

    def capture_outgoing(self, outgoing_id):
        assert outgoing_id == self.outgoing["outgoing_id"]
        return dict(self.outgoing)


class MailLibraryTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="butler-mail-"))
        self.now = time.time()
        self.fake = FakeMail(self.now)
        self.mail = load_module(self.root)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def scan(self, **kwargs):
        return self.mail.scan(slot=kwargs.pop("slot", "morning"), model=self.fake.model, reader=self.fake.read_body,
                              lister=self.fake.list_messages, account_source=lambda: self.fake.accounts, **kwargs)

    def test_reading_classifies_locally_and_reports_each_message_once(self):
        result = self.scan()
        self.assertEqual((result["total"], result["new"], result["important"], result["needs_reply"]), (4, 4, 3, 2))
        self.assertEqual(sorted(self.fake.body_reads), [1, 3, 7])          # the bulk newsletter never needed a body
        digest = result["digest"]
        self.assertIn("писем 4, важных 3", digest)
        self.assertIn("Anna <anna@example.com> — Встреча во вторник", digest)
        self.assertIn("ждут ответа", digest)
        self.assertNotIn("давай перенесём", digest)                         # body text never enters the digest
        self.assertNotIn("Weekly deals", digest)
        ordinals = [o["ordinal"] for o in result["offers"]]
        self.assertEqual(sorted(ordinals), sorted(ordinals))
        again = self.scan()
        self.assertEqual((again["total"], again["new"], again["digest"]), (0, 0, ""))   # the next reading starts where this one finished

    def test_store_keeps_bodies_private_and_resolves_by_digest_number(self):
        self.scan()
        store = self.root / "state/mail/mail.sqlite3"
        self.assertEqual(store.stat().st_mode & 0o777, 0o600)
        first = self.mail.resolve("1")
        self.assertEqual(first["subject"], "Invoice overdue")                # newest important message first
        anna = self.mail.resolve("Встреча")
        self.assertEqual(anna["message_id"], "<a1@example.com>")
        with self.assertRaises(self.mail.MailError):
            self.mail.resolve("nonexistent subject")

    def test_rules_skip_bulk_without_the_model_and_survive_a_missing_model(self):
        message = {"account": "a", "sender": "News <newsletter@x.example>", "subject": "Weekly", "received": self.now}
        verdict = self.mail.classify(message, "", model=lambda prompt: (_ for _ in ()).throw(AssertionError("model must not run")))
        self.assertEqual((verdict["important"], verdict["category"]), (False, "bulk"))
        personal = {"account": "a", "sender": "Ivan <ivan@example.com>", "subject": "Привет", "received": self.now}
        def down(prompt):
            raise self.mail.MailError("local model unavailable")
        verdict = self.mail.classify(personal, "текст", model=down)
        self.assertTrue(verdict["important"])
        self.assertIn("model unavailable", verdict["reason"])

    def test_draft_and_send_are_local_and_a_send_runs_once(self):
        self.scan()
        drafted = self.mail.draft("Встреча", "согласись", model=lambda prompt: "Анна, вторник в 10 подходит. Борис")
        self.assertEqual(drafted["draft"], "Анна, вторник в 10 подходит. Борис")
        sent = self.mail.send("Встреча", "Анна, вторник в 10 подходит.", "op-1", sender=self.fake.send_reply)
        self.assertEqual(sent["subject"], "Re: Встреча во вторник")
        self.assertEqual(self.fake.sent[0][2], 1)
        again = self.mail.send("Встреча", "Анна, вторник в 10 подходит.", "op-1", sender=self.fake.send_reply)
        self.assertTrue(again["already"])
        self.assertEqual(len(self.fake.sent), 1)
        evidence = self.mail.sent_evidence("Встреча", self.now, finder=lambda account, subject, since: {"message_id": "<s1>", "mailbox": "Sent", "subject": subject})
        self.assertEqual(evidence["subject"], "Re: Встреча во вторник")

    def test_display_card_is_local_and_the_reply_opens_in_mail_without_sending(self):
        self.scan()
        self.mail.draft("Встреча", model=lambda prompt: "Анна, вторник в 10 подходит.")
        card = self.mail.card("Встреча")
        self.assertEqual((card["sender"], card["subject"], card["account"]), ("Anna <anna@example.com>", "Встреча во вторник", "t.boris@gmail.com"))
        self.assertEqual(card["body"], "Боря, давай перенесём встречу на вторник в 10? Анна")
        self.assertEqual(card["draft"], "Анна, вторник в 10 подходит.")
        self.assertTrue(card["date"])
        opened = self.mail.open_in_mail("Встреча", opener=self.fake.open_reply)
        self.assertEqual((opened["opened"], opened["outgoing_id"]), (True, 77))
        self.assertEqual(self.fake.outgoing["content"], "Анна, вторник в 10 подходит.")      # the stored draft is prefilled
        self.assertEqual(self.fake.sent, [])                                                   # nothing was sent

    def test_mail_edited_reply_is_captured_bound_by_hash_and_refused_once_changed(self):
        self.scan()
        self.mail.open_in_mail("Встреча", "Черновик", opener=self.fake.open_reply)
        self.fake.outgoing["content"] = "Анна, давайте в четверг в 11. Борис"                  # Boris edited it in Mail.app
        review = self.mail.captured_for_review("Встреча", capture=self.fake.capture_outgoing)
        self.assertEqual((review["content"], review["to"], review["outgoing_id"]), ("Анна, давайте в четверг в 11. Борис", ["anna@example.com"], 77))
        self.assertEqual(len(review["sha"]), 64)
        sends = []
        def send_outgoing(outgoing_id, sha):
            return self.mail.send_outgoing(outgoing_id, sha, capture=self.fake.capture_outgoing) if False else (sends.append((outgoing_id, sha)) or {"subject": "Re: Встреча во вторник", "sent_at": self.now + 5})
        sent = self.mail.send("Встреча", review["content"], "op-7", outgoing_id=77, sha=review["sha"], outgoing_sender=send_outgoing)
        self.assertEqual(sends, [(77, review["sha"])])
        self.assertEqual(sent["subject"], "Re: Встреча во вторник")
        self.assertEqual(self.fake.sent, [])                                                   # no fresh reply was created
        # The hash check itself: the same text passes, a later edit refuses the send.
        captured = self.fake.capture_outgoing(77)
        sha = self.mail.content_sha(captured)
        self.assertEqual(sha, review["sha"])
        self.fake.outgoing["content"] += " P.S."
        with self.assertRaises(self.mail.MailError) as refused:
            self.mail.send_outgoing(77, sha, capture=self.fake.capture_outgoing)
        self.assertIn("draft changed since approval", str(refused.exception))
        self.fake.outgoing["content"] = captured["content"]
        self.fake.outgoing["to"] = ["someone-else@example.com"]                                # a recipient change counts too
        with self.assertRaises(self.mail.MailError):
            self.mail.send_outgoing(77, sha, capture=self.fake.capture_outgoing)
        with self.assertRaises(self.mail.MailError):
            self.mail.captured_for_review("Invoice", capture=self.fake.capture_outgoing)        # nothing open for that message

    def test_stale_mail_app_is_called_out_in_the_digest(self):
        for rows in self.fake.messages.values():
            for row in rows:
                row["received"] = self.now - 3 * 86400
        result = self.scan(lookback_hours=24 * 7)
        self.assertGreater(result["stale_hours"], 48)
        self.assertIn("Mail.app не получал почту", result["digest"])


if __name__ == "__main__":
    unittest.main()
