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


class PendingApprovalsTest(CoreCase):
    """BUG-021: what waits for the owner is listed every day until it is answered or expires."""

    def waiting(self, key, **params):
        return self.core.submit(self.request(kind="test.gated", key=key, **params), "system2")

    def digests(self):
        return self.core.store.query("SELECT * FROM deliveries WHERE idempotency_key LIKE 'attention:%'"
                                     " ORDER BY idempotency_key")

    def test_the_listing_is_oldest_first_with_age_and_the_commands(self):
        self.clock.now = at(5, 12)
        first = self.waiting("a", value=1)
        self.clock.now = at(7, 12)
        second = self.waiting("b", value=2)
        self.clock.now = at(9, 12)
        rows = self.core.attention.pending()
        self.assertEqual([r["operation"] for r in rows], [first["id"], second["id"]])
        self.assertEqual([r["age_days"] for r in rows], [4, 2])
        code = rows[0]["approve"].rsplit("_", 1)[1]
        self.assertEqual(rows[0]["approve"], f"/approve_{first['id'][3:11]}_{code}")
        self.assertEqual(rows[0]["reject"], f"/reject_{first['id'][3:11]}")
        self.assertEqual(rows[0]["kind"], "test.gated")

    def test_waiting_operations_are_in_the_daily_digest_until_answered(self):
        self.clock.now = at(5, 12)
        op = self.waiting("a")
        self.clock.now = at(7, 13)                                      # two full days and an hour
        self.assertTrue(self.core.attention.maybe_send()["sent"])
        content = self.digests()[0]["content"]
        self.assertIn("Ждут вашего подтверждения", content)
        self.assertIn(f"/approve_{op['id'][3:11]}_", content)
        self.assertIn("test.gated", content)
        self.assertIn("2 дн", content)
        self.core.delivery.send(self.digests()[0]["id"])
        self.clock.now = at(8, 9)                                       # still waiting: the reminder comes again
        self.assertTrue(self.core.attention.maybe_send()["sent"])
        code = self.core.operations.list(["awaiting_approval"])[0]["review_code"]
        self.core.approvals.grant(op["id"], code, OWNER, "telegram:butler")
        self.core.delivery.send(self.digests()[1]["id"])
        self.clock.now = at(9, 9)                                       # answered: nothing left to say
        self.assertFalse(self.core.attention.maybe_send()["sent"])

    def test_a_long_queue_shows_the_oldest_ten_and_the_count(self):
        self.clock.now = at(1, 12)
        for n in range(14):
            self.waiting(f"k{n}", value=n)
            self.clock.advance(60)
        self.clock.now = at(3, 9)
        self.core.attention.maybe_send()
        content = self.digests()[0]["content"]
        self.assertEqual(content.count("/approve_"), 10)
        self.assertIn("ещё 4", content)
        self.assertIn("14", content)

    def test_expired_operations_are_reported_once_and_grouped_by_kind(self):
        self.clock.now = at(1, 12)
        for n in range(3):
            self.waiting(f"k{n}", value=n)
        self.clock.now = at(10, 9)
        self.assertEqual(self.core.expire_stale_approvals(), {"expired": 3})
        self.assertTrue(self.core.attention.maybe_send()["sent"])
        content = self.digests()[0]["content"]
        self.assertIn("Истекли без подтверждения", content)
        self.assertIn("не выполнены", content)
        self.assertIn("test.gated ×3", content)
        self.core.delivery.send(self.digests()[0]["id"])
        self.clock.now = at(11, 9)
        self.assertFalse(self.core.attention.maybe_send()["sent"])

    def test_the_server_offers_the_listing_to_every_caller_kind_that_may_read_operations(self):
        from butler_core.server import Server
        server = Server(self.core, self.tmp / "core.sock")
        self.assertIn("approval.pending", server.methods)
