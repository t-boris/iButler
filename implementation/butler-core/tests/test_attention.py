"""BUG-021 / BUG-022: one short daily message that says what needs the owner.

Deliveries whose outcome nobody could confirm are never resent (except approval requests, see test_delivery). They
used to sit in the database unseen; the owner decided on 2026-11-10 that they are listed once, in the next daily digest.
"""
import datetime as dt
from zoneinfo import ZoneInfo

from support import OWNER, TANYA, CoreCase

ZONE = ZoneInfo("America/Chicago")


def at(day, hour, minute=0):
    return dt.datetime(2026, 11, day, hour, minute, tzinfo=ZONE).timestamp()


class AttentionTest(CoreCase):
    def uncertain(self, key, kind="status", text="✅ video готово"):
        self.transport.script = ["ambiguous"]
        delivery = self.core.delivery.enqueue(recipient=OWNER, channel="telegram:butler", persona="butler", kind=kind,
                                              content=text, idempotency_key=key)
        self.assertEqual(self.core.delivery.send(delivery["id"]), "uncertain")
        return delivery

    def digests(self):
        return self.core.store.query("SELECT * FROM deliveries WHERE idempotency_key LIKE 'attention:%'"
                                     " ORDER BY idempotency_key")

    def test_an_uncertain_delivery_is_listed_once_in_the_next_daily_digest(self):
        self.clock.now = at(8, 14, 26)
        self.uncertain("status:1", text="✅ video (4ab2cd) done in 3600s")
        self.clock.now = at(9, 9)
        self.assertEqual(self.core.attention.maybe_send()["sent"], True)
        (digest,) = self.digests()
        self.assertEqual(digest["kind"], "digest")
        self.assertEqual(digest["idempotency_key"], "attention:2026-11-09")
        self.assertIn("не подтверждена", digest["content"])
        self.assertIn("video (4ab2cd)", digest["content"])
        self.assertIn("8 нояб 14:26", digest["content"])
        self.assertEqual(self.core.delivery.send(digest["id"]), "delivered")
        # Once delivered, the same entry is not listed again, today or on the following days.
        self.assertEqual(self.core.attention.maybe_send()["sent"], False)
        self.clock.now = at(10, 9)
        self.assertEqual(self.core.attention.maybe_send()["sent"], False)
        self.assertEqual(len(self.digests()), 1)

    def test_nothing_is_sent_when_nothing_needs_attention(self):
        self.clock.now = at(9, 9)
        self.assertEqual(self.core.attention.maybe_send(), {"sent": False, "reason": "nothing to report"})
        self.assertEqual(self.digests(), [])

    def test_the_digest_waits_for_the_end_of_quiet_hours(self):
        self.clock.now = at(8, 23, 30)
        self.uncertain("status:late")
        self.clock.now = at(9, 6)
        self.assertEqual(self.core.attention.maybe_send(), {"sent": False, "reason": "quiet hours"})
        self.clock.now = at(9, 8, 5)
        self.assertEqual(self.core.attention.maybe_send()["sent"], True)

    def test_a_digest_whose_own_delivery_is_uncertain_is_repeated_the_next_day(self):
        self.clock.now = at(8, 14)
        self.uncertain("status:1")
        self.clock.now = at(9, 9)
        self.core.attention.maybe_send()
        self.transport.script = ["ambiguous"]
        self.assertEqual(self.core.delivery.send(self.digests()[0]["id"]), "uncertain")
        self.clock.now = at(10, 9)
        self.assertEqual(self.core.attention.maybe_send()["sent"], True)
        self.assertEqual([r["idempotency_key"] for r in self.digests()], ["attention:2026-11-09", "attention:2026-11-10"])
        self.assertNotIn("attention:", self.digests()[1]["content"])            # its own failed copy is not an entry

    def test_resent_approvals_and_imported_history_are_not_listed(self):
        self.clock.now = at(8, 14)
        self.uncertain("approval:op-1:abc123", kind="approval", text="Нужно подтверждение")
        self.core.delivery.import_uncertain([{"key": "old-1", "channel": "telegram:butler", "attempts": 1}])
        self.clock.now = at(9, 9)
        self.assertEqual(self.core.attention.maybe_send()["sent"], False)

    def test_the_list_is_capped_and_says_how_many_more(self):
        self.clock.now = at(8, 14)
        for n in range(13):
            self.uncertain(f"status:{n}", text=f"line {n}")
            self.clock.advance(60)
        self.clock.now = at(9, 9)
        self.core.attention.maybe_send()
        content = self.digests()[0]["content"]
        self.assertEqual(content.count("•"), 10)
        self.assertIn("ещё 3", content)

    def test_a_peers_private_text_is_not_shown_to_the_owner_of_another_persona(self):
        self.clock.now = at(8, 14)
        self.transport.script = ["ambiguous"]
        delivery = self.core.delivery.enqueue(recipient=TANYA, channel="telegram:tetyana-dyck",
                                              persona="tetyana-dyck", kind="reply", content="личное сообщение Тани",
                                              idempotency_key="tanya:1")
        self.core.delivery.send(delivery["id"])
        self.clock.now = at(9, 9)
        self.core.attention.maybe_send()
        content = self.digests()[0]["content"]
        self.assertIn("tetyana-dyck", content)
        self.assertNotIn("личное сообщение", content)

    def test_the_core_runs_the_digest_periodically(self):
        self.core.start(workers=False)
        self.assertIn("attention", [entry[2] for entry in self.core.scheduler.periodic])
