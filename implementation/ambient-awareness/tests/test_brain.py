"""DEC-067: the owner's addressed words go to the butler agent over the gateway; media and other voices stay local."""
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock

from butler_ambient.brain import AgentBrain, BrainUnavailable, parse_sse, voice_preamble


class FakeResponse:
    def __init__(self, status, lines):
        self.status, self.lines = status, list(lines)
    def readline(self, limit=-1):
        return self.lines.pop(0) if self.lines else b""
    def read(self, n=-1):
        return b"".join(self.lines)


class FakeConnection:
    requests = []
    status = 200
    lines = [b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n', b"\n",
             b'data: {"choices":[{"delta":{"content":"\xd0\x94\xd0\xb0. "}}]}\n',
             b'data: {"choices":[{"delta":{"content":"\xd0\x93\xd0\xbe\xd1\x82\xd0\xbe\xd0\xb2."}}]}\n', b"data: [DONE]\n"]
    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
    def request(self, method, path, body, headers):
        FakeConnection.requests.append({"method": method, "path": path, "body": json.loads(body), "headers": headers,
                                        "host": self.host, "port": self.port})
    def getresponse(self):
        return FakeResponse(FakeConnection.status, FakeConnection.lines)
    def close(self):
        pass


class BrainTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temp.name) / "openclaw.json"
        self.config_path.write_text(json.dumps({"gateway": {"auth": {"mode": "token", "token": "secret-token"}}}))
        self.store = Mock(); self.store.get.side_effect = lambda key, default=None: {"voice_brain": "agent"}.get(key, default)
        self.brain = AgentBrain({"openclaw_config": str(self.config_path)}, self.store, clock=lambda: 1000.0, connection=FakeConnection)
        FakeConnection.requests, FakeConnection.status = [], 200
        self.default_lines = list(FakeConnection.lines)

    def tearDown(self):
        FakeConnection.lines = self.default_lines
        self.temp.cleanup()

    def test_parse_sse_concatenates_deltas_and_stops_at_done(self):
        text = parse_sse([b'data: {"choices":[{"delta":{"content":"a"}}]}', b"", b'data: {"choices":[{"delta":{"content":"b"}}]}',
                          b"data: [DONE]", b'data: {"choices":[{"delta":{"content":"never"}}]}'])
        self.assertEqual(text, "ab")

    def test_preamble_states_facts_in_words_only(self):
        text = voice_preamble("09:30", alone=True, foreground_app="MarkView")
        self.assertIn("владелец один", text); self.assertIn("MarkView", text); self.assertIn("озвучен", text)
        self.assertIn("рядом есть кто-то ещё", voice_preamble("09:30", alone=False, company=True))

    def test_ask_sends_words_session_and_model_override_and_returns_the_text(self):
        reply = self.brain.ask("Что такое аренда?", "[контекст]")
        self.assertEqual(reply, "Да. Готов.")
        request = FakeConnection.requests[-1]
        self.assertEqual((request["host"], request["port"], request["path"]), ("127.0.0.1", 18789, "/v1/chat/completions"))
        self.assertEqual(request["headers"]["Authorization"], "Bearer secret-token")
        self.assertEqual(request["headers"]["x-openclaw-session-key"], "agent:butler:voice")
        self.assertEqual(request["headers"]["x-openclaw-model"], "anthropic/claude-sonnet-5")
        self.assertEqual(request["body"]["model"], "openclaw/butler")
        self.assertTrue(request["body"]["stream"])
        self.assertEqual(request["body"]["messages"], [{"role": "user", "content": "[контекст]\nЧто такое аренда?"}])
        self.assertEqual((self.brain.calls, self.brain.failures), (1, 0))
        self.assertIsNotNone(self.brain.snapshot()["last_ms"])
        self.assertNotIn("secret-token", json.dumps(self.brain.snapshot()))

    def test_frames_travel_as_image_parts_and_only_when_given(self):
        self.brain.ask("Что ты видишь?", "[контекст]", images=[b"\xff\xd8jpeg"])
        content = FakeConnection.requests[-1]["body"]["messages"][0]["content"]
        self.assertEqual(content[0], {"type": "text", "text": "[контекст]\nЧто ты видишь?"})
        self.assertTrue(content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,"))
        self.assertEqual(len(content), 2)

    def test_gateway_error_empty_reply_and_missing_token_raise_unavailable(self):
        FakeConnection.status = 500
        with self.assertRaises(BrainUnavailable): self.brain.ask("Привет", "[к]")
        FakeConnection.status, FakeConnection.lines = 200, [b"data: [DONE]\n"]
        with self.assertRaises(BrainUnavailable): self.brain.ask("Привет", "[к]")
        self.config_path.write_text("{}"); self.brain._token = (None, float("-inf"))
        with self.assertRaises(BrainUnavailable): self.brain.ask("Привет", "[к]")
        self.assertEqual(self.brain.failures, 3)

    def test_no_reply_is_silence_not_a_word(self):
        from butler_ambient.brain import BrainSilent
        FakeConnection.lines = [b'data: {"choices":[{"delta":{"content":"NO_REPLY"}}]}\n', b"data: [DONE]\n"]
        with self.assertRaises(BrainSilent): self.brain.ask("Ну ладно", "[к]")
        self.assertEqual(self.brain.failures, 0)

    def test_an_error_text_from_the_gateway_is_a_failure_not_an_answer(self):
        FakeConnection.lines = [b'data: {"choices":[{"delta":{"content":"Error: internal error"}}]}\n', b"data: [DONE]\n"]
        with self.assertRaises(BrainUnavailable) as caught: self.brain.ask("Прочитай почту", "[к]")
        self.assertTrue(str(caught.exception).startswith("agent-error"))
        self.assertEqual(self.brain.last_error, "agent-error")

    def test_a_question_during_a_detached_task_uses_an_aside_session(self):
        self.brain.session_lock.acquire()                                        # the main session is busy with long work
        try:
            self.brain.ask("Что по задачам?", "[к]")
        finally:
            self.brain.session_lock.release()
        self.assertEqual(FakeConnection.requests[-1]["headers"]["x-openclaw-session-key"], "agent:butler:voice:aside")
        self.brain.ask("И ещё", "[к]")
        self.assertEqual(FakeConnection.requests[-1]["headers"]["x-openclaw-session-key"], "agent:butler:voice")

    def test_cancel_token_stops_the_read(self):
        token = threading.Event(); token.set()
        with self.assertRaises(BrainUnavailable): self.brain.ask("Привет", "[к]", token=token)
        self.assertEqual(self.brain.last_error, "cancelled")

    def test_usage_footer_and_markdown_never_reach_the_voice(self):
        from butler_ambient.brain import clean_for_speech
        self.assertEqual(clean_for_speech("Да, слышу, всё норм.\n\n---\nbutler · today 4.3k ~$0.06 · month 142.9k ~$2.14"), "Да, слышу, всё норм.")
        self.assertEqual(clean_for_speech("**Три задачи**: `починить` и [ссылка](http://x). \n\n- раз\n- два"), "Три задачи: починить и ссылка.\nраз\nдва")
        self.assertEqual(clean_for_speech("Готово."), "Готово.")

    def test_setting_selects_agent_or_local(self):
        self.assertTrue(self.brain.enabled())
        self.store.get.side_effect = lambda key, default=None: {"voice_brain": "local"}.get(key, default)
        self.assertFalse(self.brain.enabled())
        self.store.get.side_effect = lambda key, default=None: default
        self.assertFalse(AgentBrain({}, self.store).enabled())                       # tests and fallback: local
        self.assertTrue(AgentBrain({"voice_brain": "agent"}, self.store).enabled())  # the deployment config


if __name__ == "__main__":
    unittest.main()
