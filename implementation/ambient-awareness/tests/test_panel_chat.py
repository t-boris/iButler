"""The panel's written conversation: its own agent session, no speech, no Telegram path, memory only."""
import io
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from butler_ambient import panel_chat
from butler_ambient.brain import AgentBrain, BrainSilent, BrainUnavailable, clean_for_panel, clean_for_speech
from butler_ambient.panel_chat import MAX_ENTRIES, MAX_QUEUED, MAX_TEXT, PanelChat, held_back, readable
from butler_ambient.service import Handler


def wait_for(condition, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if condition():
            return True
        time.sleep(.01)
    return False


class FakeBrain:
    session_key = "agent:butler:panel"

    def __init__(self, behaviour):
        self.behaviour, self.calls = behaviour, []

    def ask(self, text, preamble, *, images=(), token=None, on_text=None, speech=True):
        self.calls.append({"text": text, "preamble": preamble, "speech": speech, "token": token})
        return self.behaviour(text, on_text, token)


def service(now=None):
    now = now if now is not None else [1_000_000.0]
    return SimpleNamespace(clock=lambda: now[0], config={}, store=Mock(), owner_name=lambda: "Борис",
                           voice=Mock(), delivery=Mock(), dialogue=Mock(), now=now)


def answered(chat, turn):
    return wait_for(lambda: next((e for e in chat.snapshot()["entries"] if e["id"] == turn + ".r"), {}).get("state")
                    in {"answered", "silent", "failed"})


def row(chat, row_id):
    return next(e for e in chat.snapshot()["entries"] if e["id"] == row_id)


class PanelChatTests(unittest.TestCase):
    def make(self, behaviour, **kwargs):
        self.service = service(**kwargs)
        self.brain = FakeBrain(behaviour)
        chat = PanelChat(self.service, brain=self.brain)
        self.addCleanup(chat.stop)
        return chat

    # -- the happy path ---------------------------------------------------------------------

    def test_message_is_queued_at_once_and_answered_as_written_text(self):
        chat = self.make(lambda text, on_text, token: "**Готово.**\n- раз\n- два")
        sent = chat.send("  Какие у меня задачи?  ")
        self.assertEqual(sent["queued"], True)
        self.assertTrue(answered(chat, sent["id"]))
        asked, reply = row(chat, sent["id"]), row(chat, sent["id"] + ".r")
        self.assertEqual((asked["who"], asked["text"], asked["state"]), ("Борис", "Какие у меня задачи?", "sent"))
        self.assertEqual((reply["who"], reply["state"], reply["text"]), ("Butler", "answered", "**Готово.**\n- раз\n- два"))
        call = self.brain.calls[0]
        self.assertIs(call["speech"], False)
        self.assertIn("Панель Butler", call["preamble"])
        self.assertNotIn("озвучен", call["preamble"])

    def test_nothing_is_spoken_and_nothing_is_delivered(self):
        chat = self.make(lambda text, on_text, token: "Ответ.")
        self.assertTrue(answered(chat, chat.send("привет")["id"]))
        self.assertEqual(self.service.voice.mock_calls, [])
        self.assertEqual(self.service.delivery.mock_calls, [])
        self.assertEqual(self.service.dialogue.mock_calls, [])

    def test_answer_streams_in_and_the_silence_sentinel_is_not_shown(self):
        gate, seen = threading.Event(), []

        def behaviour(text, on_text, token):
            on_text("NO_RE")
            seen.append(chat.snapshot()["entries"][-1]["text"])         # the answer row is the newest one
            on_text("PLY is not what I say. ")
            on_text("Вот ответ")
            gate.wait(5)
            return "NO_REPLY is not what I say. Вот ответ"

        chat = self.make(behaviour)
        turn = chat.send("x")["id"]
        self.assertTrue(wait_for(lambda: row(chat, turn + ".r")["state"] == "streaming"))
        self.assertEqual(seen, [""])                                   # held back while it could still be NO_REPLY
        self.assertIn("Вот ответ", row(chat, turn + ".r")["text"])
        gate.set()
        self.assertTrue(answered(chat, turn))

    def test_usage_footer_is_not_part_of_the_answer(self):
        footer = "Ответ.\n\n---\nbutler · today 4.3k ~$0.06 · month 1.2M ~$3.10"
        self.assertEqual(clean_for_panel(footer), "Ответ.")
        chat = self.make(lambda text, on_text, token: clean_for_panel(footer))
        turn = chat.send("x")["id"]
        self.assertTrue(answered(chat, turn))
        self.assertEqual(row(chat, turn + ".r")["text"], "Ответ.")

    # -- outcomes that are not an answer ----------------------------------------------------

    def test_silence_is_stated_and_not_a_failure(self):
        def silent(text, on_text, token):
            raise BrainSilent()
        chat = self.make(silent)
        turn = chat.send("x")["id"]
        self.assertTrue(answered(chat, turn))
        reply = row(chat, turn + ".r")
        self.assertEqual((reply["state"], reply["text"]), ("silent", panel_chat.SILENT_TEXT))
        self.assertNotIn("error", reply)

    def test_gateway_failure_is_shown_with_its_reason_and_never_answered_locally(self):
        def broken(text, on_text, token):
            raise BrainUnavailable("gateway http 502")
        chat = self.make(broken)
        turn = chat.send("x")["id"]
        self.assertTrue(answered(chat, turn))
        reply = row(chat, turn + ".r")
        self.assertEqual((reply["state"], reply["text"]), ("failed", ""))
        self.assertEqual(reply["error"], "шлюз OpenClaw ответил ошибкой (502)")
        self.assertEqual(len(self.brain.calls), 1)                      # no second, local attempt

    def test_unexpected_error_reports_the_class_and_never_the_message_or_the_text(self):
        def bad(text, on_text, token):
            raise KeyError("секретный текст владельца")
        chat = self.make(bad)
        with self.assertLogs("butler.ambient", level="INFO") as logged:
            turn = chat.send("секретный текст владельца")["id"]
            self.assertTrue(answered(chat, turn))
        self.assertIn("KeyError", row(chat, turn + ".r")["error"])
        self.assertNotIn("секретный", "\n".join(logged.output))
        self.assertNotIn("секретный", row(chat, turn + ".r")["error"])

    def test_log_lines_carry_the_outcome_and_the_length_but_not_the_text(self):
        chat = self.make(lambda text, on_text, token: "Ответ с личным содержанием")
        with self.assertLogs("butler.ambient", level="INFO") as logged:
            turn = chat.send("личный вопрос")["id"]
            self.assertTrue(answered(chat, turn))
        text = "\n".join(logged.output)
        self.assertIn("accepted (13 characters)", text)
        self.assertNotIn("личный", text)

    def test_readable_reasons(self):
        self.assertIn("токен", readable(BrainUnavailable("gateway token unavailable")))
        self.assertIn("не отвечает", readable(BrainUnavailable("ConnectionRefusedError")))
        self.assertIn("не подтверждён", readable(BrainUnavailable("agent-error: Error: internal error")))
        self.assertIn("недоступен", readable(BrainUnavailable("SomethingNew")))

    # -- validation and bounds --------------------------------------------------------------

    def test_invalid_messages_are_refused_before_anything_is_queued(self):
        chat = self.make(lambda text, on_text, token: "x")
        for bad in ("", "   \n", None, 5, "a" * (MAX_TEXT + 1), "a\x00b"):
            with self.assertRaises(ValueError):
                chat.send(bad)
        self.assertEqual(chat.snapshot()["entries"], [])
        self.assertEqual(chat.send("a" * MAX_TEXT)["queued"], True)

    def test_a_full_queue_refuses_instead_of_growing(self):
        gate = threading.Event()
        chat = self.make(lambda text, on_text, token: gate.wait(5) and "x")
        chat.send("first")                                              # taken by the worker
        self.assertTrue(wait_for(lambda: chat.snapshot()["busy"] and not chat.queue))
        for index in range(MAX_QUEUED):
            chat.send(f"waiting {index}")
        with self.assertRaises(ValueError):
            chat.send("one too many")
        gate.set()

    def test_turns_run_one_at_a_time_in_order(self):
        order, gate = [], threading.Event()

        def behaviour(text, on_text, token):
            order.append(("start", text))
            gate.wait(5) if text == "one" else None
            order.append(("end", text))
            return text

        chat = self.make(behaviour)
        first, second = chat.send("one")["id"], chat.send("two")["id"]
        self.assertTrue(wait_for(lambda: order == [("start", "one")]))
        time.sleep(.1)
        self.assertEqual(order, [("start", "one")])                     # "two" has not started
        gate.set()
        self.assertTrue(answered(chat, second))
        self.assertEqual(order, [("start", "one"), ("end", "one"), ("start", "two"), ("end", "two")])

    def test_old_rows_go_after_a_day_and_the_thread_stays_bounded(self):
        chat = self.make(lambda text, on_text, token: "ok")
        turn = chat.send("old")["id"]
        self.assertTrue(answered(chat, turn))
        self.service.now[0] += panel_chat.RETENTION + 1
        self.assertEqual(chat.snapshot()["entries"], [])
        for index in range(MAX_ENTRIES):
            self.assertTrue(answered(chat, chat.send(f"m{index}")["id"]))
        self.assertLessEqual(len(chat.snapshot()["entries"]), MAX_ENTRIES)
        self.assertEqual(row(chat, chat.snapshot()["entries"][-2]["id"])["text"], f"m{MAX_ENTRIES - 1}")

    # -- clearing ---------------------------------------------------------------------------

    def test_clear_cancels_the_turn_in_flight_and_drops_its_answer(self):
        started, tokens = threading.Event(), []

        def behaviour(text, on_text, token):
            tokens.append(token)
            started.set()
            token.wait(5)
            raise BrainUnavailable("cancelled")

        chat = self.make(behaviour)
        chat.send("x")
        self.assertTrue(started.wait(5))
        self.assertEqual(chat.clear("owner")["cleared"], True)
        self.assertTrue(tokens[0].is_set())
        self.assertTrue(wait_for(lambda: not chat.snapshot()["busy"]))
        time.sleep(.05)
        self.assertEqual(chat.snapshot()["entries"], [])                # the cancelled answer never reappears
        self.assertEqual(chat.counts["failed"], 0)

    def test_the_thread_is_usable_again_after_a_clear(self):
        chat = self.make(lambda text, on_text, token: "again")
        self.assertTrue(answered(chat, chat.send("one")["id"]))
        chat.clear("owner")
        turn = chat.send("two")["id"]
        self.assertTrue(answered(chat, turn))
        self.assertEqual([e["text"] for e in chat.snapshot()["entries"]], ["two", "again"])

    def test_stop_ends_the_worker_and_refuses_new_messages(self):
        chat = self.make(lambda text, on_text, token: "x")
        self.assertTrue(answered(chat, chat.send("one")["id"]))
        chat.stop()
        self.assertFalse(chat.worker.is_alive())
        with self.assertRaises(ValueError):
            chat.send("late")


class PanelSessionTests(unittest.TestCase):
    """The real brain against a fake gateway: which session, which instructions, which text comes back."""

    def setUp(self):
        from test_brain import FakeConnection
        self.connection = FakeConnection
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        path = Path(self.temp.name) / "openclaw.json"
        path.write_text(json.dumps({"gateway": {"auth": {"mode": "token", "token": "secret-token"}}}))
        self.service = service()
        self.service.config = {"openclaw_config": str(path), "brain_session": "agent:butler:voice"}
        self.default_lines, FakeConnection.requests = list(FakeConnection.lines), []
        self.addCleanup(lambda: setattr(FakeConnection, "lines", self.default_lines))

    def chat(self):
        def factory(config, store, clock=None):
            return AgentBrain(config, store, clock=clock, connection=self.connection)
        with patch.object(panel_chat, "AgentBrain", factory):
            chat = PanelChat(self.service, follower=False)
        self.addCleanup(chat.stop)
        return chat

    def stream(self, text):
        self.connection.lines = [b'data: ' + json.dumps({"choices": [{"delta": {"content": text}}]}).encode() + b"\n", b"data: [DONE]\n"]

    def test_panel_turn_uses_its_own_session_never_the_voice_one(self):
        self.stream("Ответ.")
        chat = self.chat()
        self.assertTrue(answered(chat, chat.send("привет")["id"]))
        headers = self.connection.requests[-1]["headers"]
        self.assertEqual(headers["x-openclaw-session-key"], "agent:butler:panel")
        self.assertEqual(self.service.config["brain_session"], "agent:butler:voice")   # the voice config is untouched

    def test_panel_answer_keeps_its_markdown_and_voice_answer_does_not(self):
        text = "# Итог\n\n- **раз**\n- [ссылка](https://familybuilder.io)\n\n---\nbutler · today 4.3k ~$0.06"
        self.stream(text)
        chat = self.chat()
        turn = chat.send("итог")["id"]
        self.assertTrue(answered(chat, turn))
        written = row(chat, turn + ".r")["text"]
        self.assertIn("**раз**", written)
        self.assertIn("[ссылка](https://familybuilder.io)", written)
        self.assertNotIn("today", written)
        spoken = clean_for_speech(text)
        self.assertNotIn("**", spoken)
        self.assertNotIn("https://", spoken)

    def test_request_to_the_gateway_is_a_developer_preamble_plus_the_owner_text(self):
        self.stream("ok")
        chat = self.chat()
        self.assertTrue(answered(chat, chat.send("Что нового?")["id"]))
        body = self.connection.requests[-1]["body"]
        self.assertEqual([m["role"] for m in body["messages"]], ["developer", "user"])
        self.assertEqual(body["messages"][1]["content"], "Что нового?")
        self.assertIn("не дублируй его в Telegram", body["messages"][0]["content"])


class CommandTests(unittest.TestCase):
    """The socket commands the panel uses."""

    def run_command(self, command, args, panel_chat_object):
        class Writer(io.BytesIO):
            pass
        instance = Handler.__new__(Handler)
        instance.connection = Mock()
        instance.rfile = io.BytesIO(json.dumps({"command": command, "args": args}).encode() + b"\n")
        instance.wfile = Writer()
        instance.server = SimpleNamespace(service=SimpleNamespace(panel_chat=panel_chat_object))
        instance.handle()
        return json.loads(instance.wfile.getvalue())

    def test_send_returns_the_queue_receipt(self):
        chat = Mock(); chat.send.return_value = {"queued": True, "id": "abc"}
        self.assertEqual(self.run_command("ui-send-text", {"text": "привет"}, chat), {"result": {"queued": True, "id": "abc"}})
        chat.send.assert_called_once_with("привет")

    def test_a_refused_message_is_explained_to_the_panel_not_hidden(self):
        chat = Mock(); chat.send.side_effect = ValueError("пустое сообщение")
        self.assertEqual(self.run_command("ui-send-text", {"text": ""}, chat),
                         {"result": {"queued": False, "message": "пустое сообщение"}})

    def test_extra_or_missing_arguments_are_rejected(self):
        chat = Mock()
        for args in ({}, {"text": "a", "x": 1}, {"txt": "a"}):
            self.assertIn("error", self.run_command("ui-send-text", args, chat))
        chat.send.assert_not_called()

    def test_clear_takes_no_arguments(self):
        chat = Mock(); chat.clear.return_value = {"cleared": True, "reason": "owner"}
        self.assertEqual(self.run_command("ui-panel-chat-clear", {}, chat)["result"]["cleared"], True)
        chat.clear.assert_called_once_with("owner")
        self.assertIn("error", self.run_command("ui-panel-chat-clear", {"x": 1}, chat))


class PreambleTests(unittest.TestCase):
    """What the agent is told about its channel. The display is now part of the panel conversation, so it must not be forbidden."""

    def text(self):
        return panel_chat.panel_preamble("2026-10-26 22:50")

    def test_the_agent_is_pointed_at_the_display_and_the_playbook_for_visuals_and_buttons(self):
        text = self.text()
        for needle in ("butler_display", "docs/ambient-and-display.md", "пиши кнопки вместо вопроса словами", "карточкой"):
            self.assertIn(needle, text)

    def test_the_agent_is_no_longer_told_the_display_is_unavailable(self):
        text = self.text()
        self.assertNotIn("не нужен и недоступен", text)
        self.assertNotIn("недоступен", text)

    def test_a_refused_display_falls_back_to_text_in_the_reply(self):
        self.assertIn("если дисплей отказал, дай то же текстом", self.text())

    def test_the_channel_rules_that_were_there_before_are_kept(self):
        text = self.text()
        for needle in ("2026-10-26 22:50", "не дублируй его в Telegram", "не выдумывай прогресс", "NO_REPLY", "подтверждение одной фразой"):
            self.assertIn(needle, text)

    def test_the_preamble_stays_one_bracketed_block_and_modest_in_size(self):
        text = self.text()
        self.assertTrue(text.startswith("[Панель Butler") and text.endswith("]"))
        self.assertLess(len(text), 1400)


class HeldBackTests(unittest.TestCase):
    def test_only_a_prefix_of_the_sentinel_is_held(self):
        for prefix in ("N", "NO", "NO_RE", "[NO_REPLY]", "no reply", "NO_REPLY."):
            self.assertTrue(held_back(prefix), prefix)
        for text in ("Ответ", "No problem at all", "Нет", "NOW", "NO_REPLYX", ""):
            self.assertFalse(held_back(text), text)


if __name__ == "__main__":
    unittest.main()
