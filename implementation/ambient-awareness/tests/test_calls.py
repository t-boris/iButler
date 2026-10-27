"""DEC-068: calls are recorded as before and concluded locally after they end."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from butler_ambient import calls as module
from butler_ambient.calls import Calls, conclusions_text
from butler_ambient.store import Store


class CallsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.now = 10_000.0
        self.voice = Mock()
        self.voice.model.generate_json.return_value = {"topic": "сроки релиза", "key_points": ["релиз в пятницу"],
                                                       "decisions": ["перенести демо"], "follow_ups": ["написать Анне"], "open_questions": []}
        self.service = Mock(store=self.store, voice=self.voice, config={"butler_workspace": self.temp.name})
        self.calls = Calls(self.service, clock=lambda: self.now)
        self.calls.model = self.voice.model
        for offset in (10, 100, 200):
            mid = self.store.media("microphone", self.now + offset, self.now + offset + 1, b"x", "wav")
            self.store.save_observation(mid, [{"kind": "work-topic", "activity": "meeting", "topic": "релиз", "detail": "обсуждают сроки",
                                               "subject": "s", "identity": "", "confidence": .8}])

    def tearDown(self):
        self.store.close(); self.temp.cleanup()

    def test_short_busy_episode_is_not_a_call(self):
        self.calls.transition("busy", "mic-app", self.now)
        self.calls.transition("idle", None, self.now + 60)
        self.assertIsNone(self.calls.poll(self.now + 61))                    # still inside QUIET_AFTER
        record = self.calls.poll(self.now + 60 + module.QUIET_AFTER)
        self.assertEqual((record["concluded"], self.calls.counts.get("short")), (False, 1))

    def test_a_call_is_concluded_once_it_stays_over(self):
        self.calls.transition("busy", "mic-app", self.now)
        self.calls.transition("busy", "meeting", self.now + 5)                # the cause changes, one episode
        self.calls.transition("idle", None, self.now + 240)
        self.calls.transition("busy", "mic-app", self.now + 250)              # resumed within QUIET_AFTER: same call
        self.calls.transition("idle", None, self.now + 300)
        with patch.object(module.threading, "Thread") as thread:
            record = self.calls.poll(self.now + 300 + module.QUIET_AFTER)
        self.assertEqual((record["seconds"], record["concluded"], sorted(record["causes"])), (300, True, ["meeting", "mic-app"]))
        thread.assert_called_once()
        with patch.object(self.calls, "remember", return_value=True) as remember:
            text = self.calls.conclude(self.now, self.now + 300)
        self.assertIn("сроки релиза", text); self.assertIn("Решения: перенести демо.", text); self.assertIn("Дальше: написать Анне.", text)
        prompt = self.voice.model.generate_json.call_args.args[0]
        self.assertIn("обсуждают сроки", prompt)                                # the local facts, nothing else
        self.voice._debug.assert_called_with("call-summary", text)
        remember.assert_called_once_with(text)
        self.voice.dialogue.notice.assert_called_once()
        self.assertIn("Созвон закончился, итоги готовы: сроки релиза", self.voice.dialogue.notice.call_args.args[0])
        self.assertEqual(self.calls.last_text(), text)
        self.assertEqual(self.calls.snapshot()["summaries"], 1)

    def test_no_observations_means_no_conclusions_and_a_feed_note(self):
        self.assertIsNone(self.calls.conclude(self.now + 5000, self.now + 5300))
        self.voice.model.generate_json.assert_not_called()
        self.assertEqual(self.calls.counts.get("no-facts"), 1)

    def test_conclusions_text_skips_empty_sections(self):
        text = conclusions_text({"topic": "т", "key_points": [], "decisions": ["д"], "follow_ups": [], "open_questions": []}, self.now, self.now + 60)
        self.assertTrue(text.endswith(": т. Решения: д."))


if __name__ == "__main__":
    unittest.main()
