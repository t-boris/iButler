"""Following the panel's gateway session: results that arrive after the turn ended reach the thread, once, and only as text."""
import json
import logging
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from butler_ambient import panel_chat
from butler_ambient.brain import AgentBrain, BrainUnavailable
from butler_ambient.panel_chat import FOLLOW_LABEL, PanelChat
from butler_ambient.session_follow import LIMIT, SessionFollower

from test_panel_chat import FakeBrain, answered, row, service, wait_for


class FakeGateway:
    """An OpenClaw gateway that serves ``GET /sessions/<key>/history`` from a list of messages."""

    def __init__(self):
        self.messages, self.requests, self.status, self.fail = [], [], 200, None

    def add(self, role, content, **extra):
        seq = len(self.messages) + 1
        message = {"role": role, "content": content, "__openclaw": {"seq": seq, "id": f"m{seq}"}, **extra}
        self.messages.append(message)
        return seq

    def assistant(self, text):
        return self.add("assistant", [{"type": "text", "text": text}])

    def connection(self, host, port, timeout=None):
        gateway = self

        class Response:
            def __init__(self):
                self.status = gateway.status
                tail = gateway.messages[-LIMIT:]
                self.body = json.dumps({"sessionKey": "agent:butler:panel", "items": tail, "messages": tail,
                                        "hasMore": len(gateway.messages) > len(tail)}).encode()

            def read(self, count=None):
                return self.body if count is None else self.body[:count]

        class Connection:
            def request(self, method, path, body=None, headers=None):
                gateway.requests.append({"method": method, "path": path, "body": body, "headers": dict(headers or {})})
                if gateway.fail is not None:
                    raise gateway.fail

            def getresponse(self):
                return Response()

            def close(self):
                pass

        return Connection()


class FollowerCase(unittest.TestCase):
    def setUp(self):
        self.gateway = FakeGateway()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / "openclaw.json"
        path.write_text(json.dumps({"gateway": {"auth": {"mode": "token", "token": "secret-token"}}}))
        self.config = {"openclaw_config": str(path), "brain_session": "agent:butler:panel", "brain_model_override": ""}
        self.brain = AgentBrain(self.config, None, connection=self.gateway.connection)
        self.follower = SessionFollower(self.brain)


class SessionFollowerTests(FollowerCase):
    def test_the_read_is_one_authorised_get_of_the_panel_session_without_a_model_header(self):
        self.follower.poll()
        request = self.gateway.requests[0]
        self.assertEqual((request["method"], request["body"]), ("GET", None))
        self.assertEqual(request["path"], "/sessions/agent%3Abutler%3Apanel/history?limit=40")
        self.assertEqual(request["headers"]["Authorization"], "Bearer secret-token")
        self.assertNotIn("x-openclaw-model", request["headers"])

    def test_the_first_read_only_sets_the_cursor_old_history_is_not_news(self):
        self.gateway.assistant("старый ответ")
        self.assertEqual(self.follower.poll(), [])
        self.assertEqual(self.follower.poll(), [])

    def test_a_later_assistant_message_is_returned_once(self):
        self.follower.poll()
        self.gateway.assistant("Диск заполнен на 88%.")
        self.assertEqual(self.follower.poll(), ["Диск заполнен на 88%."])
        self.assertEqual(self.follower.poll(), [])

    def test_the_first_message_of_a_brand_new_session_is_news(self):
        self.gateway.status = 404
        self.assertEqual(self.follower.poll(), [])
        self.gateway.status = 200
        self.gateway.assistant("первое сообщение")
        self.assertEqual(self.follower.poll(), ["первое сообщение"])

    def test_only_the_assistants_words_are_returned(self):
        self.follower.poll()
        self.gateway.add("user", "[System] subagent finished")
        self.gateway.add("assistant", [{"type": "toolCall", "name": "sessions_spawn", "arguments": {"task": "секрет"}}])
        self.gateway.add("toolResult", [{"type": "toolResult", "content": "df -h вывод"}])
        self.gateway.assistant("   ")
        self.gateway.assistant("NO_REPLY")
        self.gateway.assistant("Итог.\n\nbutler · today 4.3k ~$0.06")
        self.assertEqual(self.follower.poll(), ["Итог."])

    def test_a_late_copy_of_the_answer_the_panel_already_shows_is_not_a_follow_up(self):
        self.follower.poll()
        self.gateway.assistant("Запустил фоновую проверку диска.")        # the turn's own transcript, persisted after the stream
        self.gateway.assistant("Готово: свободно 115 ГБ.")
        shown = ["Начинаю.\n\nЗапустил фоновую проверку диска."]
        self.assertEqual(self.follower.poll(shown), ["Готово: свободно 115 ГБ."])

    def test_mark_accounts_for_everything_so_far(self):
        self.follower.poll()
        self.gateway.assistant("во время моего хода")
        self.assertTrue(self.follower.mark())
        self.assertEqual(self.follower.poll(), [])

    def test_reset_starts_over_from_the_session_as_it_is_then(self):
        self.follower.poll()
        self.gateway.assistant("до очистки")
        self.follower.reset()
        self.assertEqual(self.follower.poll(), [])
        self.gateway.assistant("после очистки")
        self.assertEqual(self.follower.poll(), ["после очистки"])

    def test_more_messages_than_one_read_holds_is_counted_as_a_gap(self):
        self.follower.poll()
        for index in range(LIMIT + 5):
            self.gateway.assistant(f"сообщение {index}")
        self.assertEqual(len(self.follower.poll()), LIMIT)
        self.assertEqual(self.follower.snapshot()["counts"]["gaps"], 1)

    def test_a_gateway_that_cannot_be_read_changes_nothing_and_is_counted(self):
        self.follower.poll()
        self.gateway.assistant("придёт позже")
        expected = []
        def fails(code):
            self.assertEqual(self.follower.poll(), [])
            expected.append(code)
            snapshot = self.follower.snapshot()
            self.assertEqual((snapshot["counts"]["failures"], snapshot["last_error"]), (len(expected), code))
            self.assertNotIn("secret", json.dumps(snapshot))                  # a class or a short code, never a message
        self.gateway.status = 500
        fails("history http 500")
        self.gateway.status, self.gateway.fail = 200, ConnectionRefusedError("secret detail")
        fails("ConnectionRefusedError")
        self.gateway.fail = None
        with patch("butler_ambient.session_follow.json.loads", side_effect=ValueError("secret detail")):
            fails("ValueError")
        with patch.object(self.brain, "token", side_effect=BrainUnavailable("gateway token unavailable")):
            fails("gateway token unavailable")
        self.assertEqual(self.follower.poll(), ["придёт позже"])        # nothing was lost while it was unreadable
        self.assertIsNone(self.follower.snapshot()["last_error"])


class PanelFollowUpTests(FollowerCase):
    def setUp(self):
        super().setUp()
        patcher = patch.object(panel_chat, "FOLLOW_FAST", .02)
        patcher.start(); self.addCleanup(patcher.stop)
        slow = patch.object(panel_chat, "FOLLOW_SLOW", .02)
        slow.start(); self.addCleanup(slow.stop)

    def chat(self, behaviour, follower=None):
        self.service = service()
        self.panel_brain = FakeBrain(behaviour)
        chat = PanelChat(self.service, brain=self.panel_brain, follower=follower or self.follower)
        self.addCleanup(chat.stop)
        return chat

    def kinds(self, chat):
        return [(e["kind"], e["who"], e["text"]) for e in chat.snapshot()["entries"] if e["from"] == "butler"]

    def test_a_result_that_arrives_after_the_turn_ended_appears_in_the_thread_once(self):
        def own_turn(text, on_text, token):
            self.gateway.add("user", text)
            self.gateway.assistant("Запустил проверку, жду.")
            return "Запустил проверку, жду."
        chat = self.chat(own_turn)
        turn = chat.send("проверь диск")["id"]
        self.assertTrue(answered(chat, turn))
        self.assertTrue(wait_for(lambda: self.follower.snapshot()["cursor"] is not None))
        self.gateway.add("user", "[System] sub-agent disk_usage_scan finished")
        self.gateway.assistant("Диск заполнен на 88,4%.")
        self.assertTrue(wait_for(lambda: any(k[0] == "followup" for k in self.kinds(chat))))
        time.sleep(.15)
        late = [k for k in self.kinds(chat) if k[0] == "followup"]
        self.assertEqual(late, [("followup", FOLLOW_LABEL, "Диск заполнен на 88,4%.")])
        self.assertEqual(sum(1 for k in self.kinds(chat) if "Запустил проверку" in k[2]), 1)    # the turn's own words only once
        self.assertEqual(chat.snapshot()["counts"]["followup"], 1)

    def test_the_turns_own_transcript_persisted_a_moment_late_is_not_shown_twice(self):
        def own_turn(text, on_text, token):
            return "Ответ на вопрос."
        chat = self.chat(own_turn)
        turn = chat.send("вопрос")["id"]
        self.assertTrue(answered(chat, turn))
        self.assertTrue(wait_for(lambda: self.follower.snapshot()["cursor"] is not None))
        self.gateway.add("user", "вопрос")
        self.gateway.assistant("Ответ на вопрос.")                   # arrives after the cursor was marked
        time.sleep(.2)
        self.assertEqual([k for k in self.kinds(chat) if k[0] == "followup"], [])

    def test_a_cleared_thread_does_not_get_old_or_late_messages_back(self):
        chat = self.chat(lambda text, on_text, token: "ок")
        self.assertTrue(answered(chat, chat.send("раз")["id"]))
        self.assertTrue(wait_for(lambda: self.follower.snapshot()["cursor"] is not None))
        chat.clear("owner")
        self.gateway.assistant("сообщение после очистки")
        time.sleep(.2)
        self.assertEqual(chat.snapshot()["entries"], [])
        self.assertIsNone(self.follower.snapshot()["cursor"])

    def test_nothing_is_read_while_the_turn_is_streaming(self):
        release = []
        def slow(text, on_text, token):
            wait_for(lambda: release, timeout=5)
            return "готово"
        chat = self.chat(slow)
        turn = chat.send("долго")["id"]
        self.assertTrue(wait_for(lambda: self.follower.snapshot()["cursor"] is not None))     # the read before the turn is done
        before = len(self.gateway.requests)
        time.sleep(.3)                                                                        # fifteen follow intervals
        self.assertEqual(len(self.gateway.requests), before)
        release.append(1)
        self.assertTrue(answered(chat, turn))

    def test_a_result_that_arrived_just_before_the_next_turn_is_shown_not_swallowed(self):
        with patch.object(panel_chat, "FOLLOW_FAST", 60.0), patch.object(panel_chat, "FOLLOW_SLOW", 60.0):
            chat = self.chat(lambda text, on_text, token: "ок")
            self.assertTrue(answered(chat, chat.send("первый")["id"]))
            self.gateway.assistant("результат фоновой задачи")
            self.assertTrue(answered(chat, chat.send("второй")["id"]))
            self.assertTrue(wait_for(lambda: any(k[2] == "результат фоновой задачи" for k in self.kinds(chat))))

    def test_a_message_the_stream_did_not_carry_but_the_turn_recorded_is_not_a_follow_up(self):
        def own_turn(text, on_text, token):
            self.gateway.assistant("Промежуточная заметка агента.")           # recorded during the turn, not part of the reply
            return "Итог."
        chat = self.chat(own_turn)
        self.assertTrue(answered(chat, chat.send("вопрос")["id"]))
        self.assertTrue(wait_for(lambda: self.follower.snapshot()["cursor"] is not None))
        time.sleep(.2)
        self.assertEqual([k for k in self.kinds(chat) if k[0] == "followup"], [])

    def test_the_answer_a_failed_turn_finished_later_is_shown_as_a_follow_up(self):
        from butler_ambient.brain import BrainUnavailable as Unavailable
        def broken_stream(text, on_text, token):
            self.gateway.assistant("Готово: диск проверен.")                  # the agent finished although the stream broke
            raise Unavailable("TimeoutError")
        chat = self.chat(broken_stream)
        turn = chat.send("проверь диск")["id"]
        self.assertTrue(answered(chat, turn))
        self.assertEqual(row(chat, turn + ".r")["state"], "failed")
        self.assertTrue(wait_for(lambda: any(k[0] == "followup" for k in self.kinds(chat))))
        self.assertEqual([k[2] for k in self.kinds(chat) if k[0] == "followup"], ["Готово: диск проверен."])

    def test_a_clear_that_lands_between_the_read_and_the_append_drops_the_late_message(self):
        holder = {}
        class ClearingFollower:
            def poll(self, shown=()):
                holder["chat"].clear("owner")             # the owner clears while the gateway read is in flight
                return ["поздний ответ"]
            def mark(self): return True
            def reset(self): pass
            def snapshot(self): return {}
        chat = self.chat(lambda text, on_text, token: "ок", follower=ClearingFollower())
        holder["chat"] = chat
        chat._drain()
        self.assertEqual(chat.snapshot()["entries"], [])
        self.assertEqual(chat.snapshot()["counts"]["followup"], 0)

    def test_log_lines_never_carry_message_text(self):
        with self.assertLogs("butler.ambient", level="INFO") as logs:
            chat = self.chat(lambda text, on_text, token: "ок")
            self.assertTrue(answered(chat, chat.send("секретный вопрос")["id"]))
            self.assertTrue(wait_for(lambda: self.follower.snapshot()["cursor"] is not None))
            self.gateway.assistant("секретный результат")
            self.assertTrue(wait_for(lambda: any(k[0] == "followup" for k in self.kinds(chat))))
        joined = "\n".join(logs.output)
        self.assertNotIn("секрет", joined)
        self.assertIn("follow-up received (1)", joined)

    def test_answer_is_published_after_its_follow_cursor_is_finalized(self):
        marked,release=threading.Event(),threading.Event()
        self.addCleanup(release.set)
        real_mark=self.follower.mark
        def held_mark():
            result=real_mark();marked.set();release.wait(5);return result
        with patch.object(self.follower,'mark',side_effect=held_mark):
            chat=self.chat(lambda text,on_text,token:'готово')
            turn=chat.send('вопрос')['id']
            self.assertTrue(marked.wait(2))
            self.assertNotEqual(row(chat,turn+'.r')['state'],'answered')
            release.set()
            self.assertTrue(answered(chat,turn))
            self.gateway.assistant('следующий результат')
            self.assertTrue(wait_for(lambda:any(k[2]=='следующий результат' for k in self.kinds(chat))))


class PanelModelTests(unittest.TestCase):
    def test_the_panel_uses_the_agents_own_model_unless_configured_and_voice_keeps_its_override(self):
        def brain_for(config):
            svc = service(); svc.config = config
            captured = {}
            def factory(cfg, store, clock=None):
                captured.update(cfg); return FakeBrain(lambda *a: "")
            with patch.object(panel_chat, "AgentBrain", factory):
                chat = PanelChat(svc, follower=False)
            chat.stop()
            return captured
        voice_like = {"brain_model_override": "anthropic/claude-sonnet-5"}
        self.assertEqual(brain_for(dict(voice_like))["brain_model_override"], "")
        self.assertEqual(brain_for(dict(voice_like, panel_model_override="anthropic/claude-opus-4-8"))["brain_model_override"],
                         "anthropic/claude-opus-4-8")


if __name__ == "__main__":
    unittest.main()
