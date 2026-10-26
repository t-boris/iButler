"""Everyday scenes for the conversational skill (REQ-029…035): guest beside, answer without the name, silence,
backchannel, unfinished thought, correction. Models and speech are faked; nothing is recorded."""
import datetime as dt
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from butler_ambient.conversation import Canceled, Conversation
from butler_ambient.delivery import ZONE
from butler_ambient.dialogue_state import Scene
from butler_ambient.policy import Evidence
from butler_ambient.service import Service
from butler_ambient.store import Store


class SceneCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.now = dt.datetime.now(ZONE).replace(hour=12, minute=0, second=0, microsecond=0).timestamp()
        self.inputs = Mock(audio_status={"ready": True, "echo_cancellation": True}, playback={"state": "idle", "id": None})
        self.service = Service(self.store, self.inputs, Mock(), Mock(), clock=lambda: self.now, transport=Mock())
        self.voice = Conversation(self.service)
        self.service.voice = self.voice
        self.voice.model = Mock()
        self.voice.barge_model = Mock()
        self.store.execute("INSERT INTO agreements VALUES ('owner','owner',1,'{}')")
        self.service.gate.enable(True, self.now)
        for _ in range(3):
            self.now += .1
            self.service.gate.check(self.evidence(), self.now)
            self.service.dialogue_gate.check(self.evidence(), self.now, True)
        self.voice.token = threading.Event()
        self.spoken = []
        self.voice.speak = Mock(side_effect=lambda text, *a, **k: self.spoken.append(text) or "spoken")

    def tearDown(self):
        self.service.stop()
        self.store.close()
        self.temp.cleanup()

    def evidence(self, speakers=frozenset({"owner"})):
        return Evidence(self.now, True, True, frozenset({"owner"}), True, speakers, frozenset({"owner"}), True, True, True)

    def turn(self, text, *, addressed=True, confidence=.99, alone=True, reply="Да, я здесь.", scene=None):
        # The lease is re-checked every batch in the live service; refresh it the same way here.
        for _ in range(3):
            self.now += .1
            self.service.gate.check(self.evidence(), self.now)
            self.service.dialogue_gate.check(self.evidence(), self.now, True)
        self.voice.model.transcribe.return_value = text
        self.voice.model.generate_json.side_effect = [
            {"addressed": addressed, "confidence": confidence, "intent": "chat", "criteria": "", "telegram": False, "reply": reply}]
        self.voice._turn(b"\0\0" * 16000, self.voice.token, self.service.voice_gate.generation, alone, scene=scene)

    def outcomes(self):
        return [e["detail"] for e in self.voice.debug_events if e["event"] == "outcome"]


class AddresseeScenes(SceneCase):
    def test_guest_beside_and_owner_turned_away_without_the_name_stays_silent(self):
        scene = Scene(available=True, faces=2, owner_visible=True, owner_facing=False, owner_talking=True, others_present=True)
        self.turn("Ты завтра придёшь?", alone=False, scene=scene)
        self.assertEqual(self.spoken, [])
        self.assertIn("молчу · зрение учтено", self.outcomes()[-1])
        self.assertEqual(self.voice.metrics.snapshot()["vision_used"], 1)

    def test_the_name_overrides_the_turned_head(self):
        scene = Scene(available=True, faces=2, owner_visible=True, owner_facing=False, owner_talking=True, others_present=True)
        self.turn("Батлер, который час?", alone=False, scene=scene)
        self.assertEqual(self.spoken, ["Да, я здесь."])

    def test_no_image_means_the_audio_path_decides(self):
        self.turn("Как расставить приоритеты?", alone=True, scene=Scene())
        self.assertEqual(self.spoken, ["Да, я здесь."])
        self.assertFalse(self.voice.diagnostics()["scene"]["image_available"])

    def test_answer_to_butlers_own_question_needs_no_name(self):
        self.turn("Батлер, когда мне лучше позвонить Саше?", reply="А в какой день ты хочешь позвонить?")
        self.assertTrue(self.voice.session.answering("owner", self.now))
        self.now += 5
        self.voice.alone = False
        self.turn("Во вторник", alone=False, confidence=.7, reply="Во вторник лучше утром.")
        self.assertEqual(self.spoken[-1], "Во вторник лучше утром.")
        self.assertFalse(self.voice.session.answering("owner", self.now))

    def test_the_name_alone_asks_to_repeat_once(self):
        self.turn("Батлер")
        self.assertEqual(self.spoken, ["Не расслышал — повтори, пожалуйста."])
        self.assertIn("прошу повторить", self.outcomes()[-1])
        self.voice.model.generate_json.assert_not_called()


class SilenceScenes(SceneCase):
    def test_silence_request_holds_until_the_timer_or_the_name(self):
        self.turn("Батлер, помолчи")
        self.assertEqual(self.spoken, [])
        self.assertTrue(self.voice.session.quiet(self.now))
        self.turn("Как расставить приоритеты?")
        self.assertEqual(self.spoken, [])
        self.assertIn("попросили помолчать", self.outcomes()[-1])
        self.now += 601
        self.turn("Как расставить приоритеты?")
        self.assertEqual(self.spoken, ["Да, я здесь."])
        self.turn("помолчи на час")
        self.now += 100
        self.turn("Батлер, ты здесь?")
        self.assertEqual(self.spoken[-1], "Да, я здесь.")
        self.assertFalse(self.voice.session.quiet(self.now))
        self.assertIn("silence-end", [e["event"] for e in self.voice.debug_events])

    def test_not_you_counts_an_intervention_and_stays_silent(self):
        self.turn("Я не тебе говорю")
        self.assertEqual(self.spoken, [])
        self.assertEqual(self.voice.metrics.snapshot()["interventions"], 1)
        self.turn("Батлер, я тебе говорю, который час?")
        self.assertEqual(self.voice.metrics.snapshot()["corrections"], 1)
        self.assertEqual(self.spoken[-1], "Да, я здесь.")


class TurnScenes(SceneCase):
    def test_unfinished_thought_waits_and_a_continuation_supersedes_it(self):
        self.voice.model.transcribe.return_value = "Напомни мне завтра…"
        started = time.monotonic()
        threading.Timer(0.3, self.voice.token.set).start()       # the continuation arrives: feed() cancels this turn
        with self.assertRaises(Canceled):
            self.voice._turn(b"\0\0" * 16000, self.voice.token, self.service.voice_gate.generation, True)
        self.assertLess(time.monotonic() - started, 1.4)
        self.voice.model.generate_json.assert_not_called()
        self.assertIn("continuation-wait", [e["event"] for e in self.voice.debug_events])

    def test_complete_request_does_not_wait(self):
        started = time.monotonic()
        self.turn("Который час?")
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(self.spoken, ["Да, я здесь."])

    def test_backchannel_during_playback_never_stops_it(self):
        self.voice.state = "speaking"
        self.voice.last_spoken = "Сначала разберём задачи на сегодня"
        self.voice.barge_audio.extend(b"\0\0" * 8000)
        self.voice.barge_owner = True
        self.voice.barge_model.transcribe.return_value = "угу"
        self.voice.cancel = Mock()
        self.voice._barge(bytes(self.voice.barge_audio), self.voice.token, self.service.voice_gate.generation)
        self.voice.cancel.assert_not_called()
        self.assertEqual(self.voice.metrics.snapshot()["backchannels"], 1)
        self.assertIn("backchannel", [e["event"] for e in self.voice.debug_events])

    def test_real_interruption_stops_and_records_latency(self):
        self.voice.state = "speaking"
        self.voice.last_spoken = "Сначала разберём задачи на сегодня"
        self.voice.barge_audio.extend(b"\0\0" * 8000)
        self.voice.barge_owner = True
        self.voice.barge_started = time.monotonic() - 0.25
        self.voice.barge_model.transcribe.return_value = "Стоп, не то"
        self.voice._barge(bytes(self.voice.barge_audio), self.voice.token, self.service.voice_gate.generation)
        metrics = self.voice.metrics.snapshot()
        self.assertEqual(metrics["interruptions"], 1)
        self.assertGreaterEqual(metrics["stop_latency_p50"], 0.25)
        self.assertIn("interrupted", [e["event"] for e in self.voice.debug_events])


class PrivacyScenes(SceneCase):
    def test_private_answer_is_withheld_when_someone_appears(self):
        self.service.latest_scene = Scene(available=True, faces=2, owner_visible=True, owner_facing=True, others_present=True)
        self.assertTrue(self.voice._company_changed(alone=True))
        self.assertFalse(self.voice._company_changed(alone=False))
        self.service.latest_scene = Scene(available=True, faces=1, owner_visible=True, owner_facing=True, others_present=False)
        self.assertFalse(self.voice._company_changed(alone=True))

    def test_outcomes_and_session_are_visible_as_metadata(self):
        self.turn("Как расставить приоритеты?")
        diagnostics = self.voice.diagnostics()
        self.assertEqual(diagnostics["dialogue_metrics"]["outcomes"], {"answer": 1})
        self.assertTrue(diagnostics["session"]["active"])
        self.assertNotIn("приоритеты", str(diagnostics["session"]))


if __name__ == "__main__":
    unittest.main()
