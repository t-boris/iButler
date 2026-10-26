"""REQ-022/024/025: text admission with a receipt after persistence, chat turns, continuation."""
import sqlite3
import unittest

from support import OWNER, TANYA, CoreCase


def message(message_id="10", text="Привет, Батлер", account="butler", sender=OWNER[9:], **extra):
    return {"account": account, "sender": sender, "chat": sender, "message_id": message_id, "date": 1_790_000_000,
            "text": text, "session_key": f"agent:{account}:telegram:direct:{sender}", **extra}


class AdmissionTest(CoreCase):
    def test_receipt_only_after_durable_persistence(self):
        result = self.core.admit_message(message())
        self.assertTrue(result["admitted"])
        self.assertEqual(result["receipt"], "👌")
        # A second connection sees the committed task: it is on disk, not in a transaction.
        other = sqlite3.connect(self.core.store.path)
        state = other.execute("SELECT state FROM tasks WHERE id=?", (result["task"],)).fetchone()[0]
        other.close()
        self.assertEqual(state, "scheduled")
        event = self.core.store.one("SELECT payload, privacy FROM events")
        self.assertIn("Привет", event["payload"])
        self.assertEqual(event["privacy"], "raw_local_only")

    def test_same_message_is_one_task(self):
        first = self.core.admit_message(message())
        second = self.core.admit_message(message())
        self.assertTrue(second["duplicate"])
        self.assertEqual(first["task"], second["task"])
        self.assertEqual(self.core.store.one("SELECT COUNT(*) FROM tasks")[0], 1)

    def test_storage_exhaustion_rejects_explicitly(self):
        self.core.admit_message(message("1"))
        pages = self.core.store.db.execute("PRAGMA page_count").fetchone()[0]
        self.core.store.db.execute(f"PRAGMA max_page_count={pages}")
        result = self.core.admit_message(message("2", text="x" * 50000))
        self.assertFalse(result["admitted"])
        self.assertEqual(result["rejected"], "storage")
        self.assertIn("не сохранено", result["text"])
        self.assertEqual(self.core.store.one("SELECT COUNT(*) FROM tasks")[0], 1)

    def test_low_free_space_rejects_before_writing(self):
        self.core.store.reserve = 10 ** 18
        result = self.core.admit_message(message("3"))
        self.assertEqual(result.get("rejected"), "storage")

    def test_unmanaged_account_and_unknown_sender(self):
        self.assertFalse(self.core.admit_message(message(account="bamberg-news"))["admitted"])
        self.assertFalse(self.core.admit_message(message(sender="9999999"))["admitted"])

    def test_tatyana_is_recorded_in_her_isolated_persona(self):
        result = self.core.admit_message(message(account="tetyana-dyck", sender=TANYA[9:]))
        self.assertTrue(result["admitted"])
        self.assertEqual(result["persona"], "tetyana-dyck")
        listing = self.core.admit_message(message("11", text="/tasks"))["text"]
        self.assertNotIn(result["task"][3:11], listing)


class ChatTurnTest(CoreCase):
    def test_turn_lifecycle(self):
        task = self.core.admit_message(message())["task"]
        session = message()["session_key"]
        self.assertEqual(self.core.turn_started({"session_key": session, "run_id": "run-1"})["attached"], 1)
        self.assertEqual(self.core.tasks.get(task)["state"], "running")
        self.assertEqual(self.core.turn_ended({"session_key": session, "run_id": "run-1", "outcome": "end"})["closed"],
                         1)
        self.assertEqual(self.core.tasks.get(task)["state"], "succeeded")
        self.core.reply_observed({"account": "butler", "chat": OWNER[9:], "message_id": "77", "task": task})
        self.assertEqual(self.core.store.one("SELECT state FROM deliveries WHERE kind='reply'")[0], "observed")

    def test_runs_of_other_sessions_do_not_take_over(self):
        task = self.core.admit_message(message())["task"]
        self.assertEqual(self.core.turn_started({"session_key": "agent:butler:cron:x", "run_id": "c"})["attached"], 0)
        self.core.turn_ended({"session_key": "agent:butler:cron:x", "run_id": "c", "outcome": "end"})
        self.clock.advance(60)
        self.assertEqual(self.core.merge_pending(), [])
        self.assertEqual(self.core.tasks.get(task)["state"], "scheduled")

    def test_failed_turn_and_follow_up_turn(self):
        session = message()["session_key"]
        first = self.core.admit_message(message("1"))["task"]
        self.core.turn_started({"session_key": session, "run_id": "r1"})
        second = self.core.admit_message(message("2", text="ещё вопрос"))["task"]
        self.core.turn_ended({"session_key": session, "run_id": "r1", "outcome": "error", "error": "timeout"})
        self.assertEqual(self.core.tasks.get(first)["state"], "failed")
        self.clock.advance(5)
        self.core.turn_started({"session_key": session, "run_id": "r2"})     # follow-up run within the grace
        self.clock.advance(60)
        self.assertEqual(self.core.merge_pending(), [])
        self.assertEqual(self.core.tasks.get(second)["state"], "running")

    def test_message_folded_into_running_turn_is_closed_after_grace(self):
        session = message()["session_key"]
        self.core.admit_message(message("1"))
        self.core.turn_started({"session_key": session, "run_id": "r1"})
        second = self.core.admit_message(message("2", text="и ещё"))["task"]
        self.core.turn_ended({"session_key": session, "run_id": "r1", "outcome": "end"})
        self.clock.advance(10)
        self.assertEqual(self.core.merge_pending(), [])
        self.clock.advance(30)
        self.assertEqual(self.core.merge_pending(), [second])
        self.assertEqual(self.core.tasks.get(second)["state"], "succeeded")
        self.clock.advance(3 * 3600)
        self.assertEqual(self.core.interrupt_stale_turns(), [])              # no false interruption notice

    def test_gateway_restart_interrupts_without_rerunning_and_notifies(self):
        self.core.bridge_hello({"instance": "gw-a", "version": "0.1.0", "started_at": self.clock()})
        task = self.core.admit_message(message())["task"]
        self.core.turn_started({"session_key": message()["session_key"], "run_id": "run-1"})
        self.clock.advance(60)
        result = self.core.bridge_hello({"instance": "gw-b", "version": "0.1.0", "started_at": self.clock()})
        self.assertEqual(result["interrupted"], 1)
        self.assertEqual(self.core.tasks.get(task)["state"], "interrupted")
        notice = self.core.store.one("SELECT content, recipient FROM deliveries WHERE kind='status'")
        self.assertIn(f"/continue_{task[3:11]}", notice["content"])
        self.assertEqual(self.core.store.one("SELECT COUNT(*) FROM operations")[0], 0)   # nothing re-run

    def test_explicit_continuation_reuses_the_task_once(self):
        self.core.bridge_hello({"instance": "gw-a", "started_at": self.clock()})
        task = self.core.admit_message(message())["task"]
        self.clock.advance(5)
        self.core.bridge_hello({"instance": "gw-b", "started_at": self.clock()})
        reply = self.core.admit_message(message("12", text=f"/continue_{task[3:11]}"))["text"]
        self.assertIn("Продолжаю", reply)
        self.core.scheduler.tick()
        self.core.admit_message(message("13", text=f"/continue_{task[3:11]}"))
        self.core.scheduler.tick()
        ops = self.core.store.query("SELECT task, kind FROM operations")
        self.assertEqual([tuple(r) for r in ops], [(task, "openclaw.agent_turn")])
        self.assertEqual(self.core.store.one("SELECT COUNT(*) FROM tasks WHERE kind='chat'")[0], 1)

    def test_continuation_from_another_persona_is_not_disclosed(self):
        task = self.core.admit_message(message())["task"]
        reply = self.core.admit_message(message("2", account="tetyana-dyck", sender=TANYA[9:],
                                                text=f"/task_{task[3:11]}"))["text"]
        self.assertEqual(reply, "Эта задача недоступна в этом канале.")

    def test_stale_turn_is_interrupted(self):
        task = self.core.admit_message(message())["task"]
        self.core.turn_started({"session_key": message()["session_key"], "run_id": "r"})
        self.clock.advance(2 * 3600 + 1)
        self.assertEqual(self.core.interrupt_stale_turns(), [task])


class CommandTest(CoreCase):
    def test_status_shows_progress_approval_and_uncertainty(self):
        op = self.core.submit(self.request(kind="test.gated", key="g"), "system2")
        text = self.core.admit_message(message("20", text=f"/task_{op['task'][3:11]}"))["text"]
        self.assertIn("ждёт подтверждения", text)
        self.assertIn(f"/approve_{op['id'][3:11]}_{op['review_code']}", text)

    def test_cross_channel_approval_checks_authority_and_binding(self):
        op = self.core.submit(self.request(kind="test.gated", key="g"), "ambient")
        wrong = self.core.admit_message(message("21", text=f"/approve_{op['id'][3:11]}_000000"))["text"]
        self.assertIn("Не выполнено", wrong)
        intruder = self.core.admit_message(message("22", account="tetyana-dyck", sender=TANYA[9:],
                                                   text=f"/approve_{op['id'][3:11]}_{op['review_code']}"))["text"]
        self.assertIn("Не выполнено", intruder)
        ok = self.core.admit_message(message("23", text=f"/approve_{op['id'][3:11]}_{op['review_code']}"))["text"]
        self.assertIn("Подтверждено", ok)
        self.assertEqual(self.core.store.one("SELECT channel FROM approvals")[0], "telegram:butler")

    def test_resolution_command(self):
        from butler_core.adapters.testing import ScriptedAdapter
        self.core.adapters["test"] = ScriptedAdapter("test", script=["ambiguous"], reconcile_supported=False)
        op = self.core.submit(self.request(key="u"), "system2")
        self.core.operations.run(op["id"])
        text = self.core.admit_message(message("24", text=f"/resolve_{op['id'][3:11]}_done проверил в сервисе"))["text"]
        self.assertIn("Повторного выполнения не будет", text)
        self.assertEqual(self.core.operations.get(op["id"])["state"], "succeeded")


if __name__ == "__main__":
    unittest.main()
