"""Dictation: speech to text for the panel's message field, claimed from the hearing path and kept nowhere else."""
import io
import json
import queue
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from butler_ambient import dictation as dictation_module
from butler_ambient.conversation import Conversation
from butler_ambient.dictation import (KEEP_RESULT, NOTHING, NOTHING_HEARD_AFTER, SILENCE_AFTER, STOP_GRACE, TOTAL_LIMIT,
                                      Dictation)
from butler_ambient.listening import Listening
from butler_ambient.panel_chat import MAX_TEXT
from butler_ambient.policy import Evidence
from butler_ambient.service import Handler, Service
from butler_ambient.store import Store


class FakeService:
    """Just what Dictation reads: a clock, the gate, the hearing path and the microphone status."""

    def __init__(self):
        self.now = 1000.0
        self.lock = threading.RLock()
        self.gate = SimpleNamespace(enabled=True)
        self.stop_event = threading.Event()
        self.owner_setup = SimpleNamespace(capturing=False)
        self.voice = SimpleNamespace(reply_in_progress=False)
        self.listening = SimpleNamespace(enabled=True, phase="waiting", processing=False, jobs=queue.Queue(), audio=bytearray())
        self.inputs = SimpleNamespace(audio_status={"input_muted": False, "input_volume": 0.6},
                                      input_signal={"at": self.now, "rms_dbfs": -30.0})

    def clock(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds
        self.inputs.input_signal = {"at": self.now, "rms_dbfs": -30.0}


class StateMachineTests(unittest.TestCase):
    def setUp(self):
        self.service = FakeService()
        self.dictation = Dictation(self.service)

    # -- starting ---------------------------------------------------------------------------

    def test_start_listens_and_a_second_start_is_the_same_dictation(self):
        first = self.dictation.start()
        self.assertEqual((first["state"], first["text"]), ("listening", ""))
        self.assertTrue(self.dictation.listening())
        self.assertEqual(self.dictation.start()["id"], first["id"])

    def test_every_reason_it_cannot_start_is_stated_and_nothing_is_claimed(self):
        cases = {
            "paused": (lambda s: setattr(s.gate, "enabled", False), "паузе"),
            "stopping": (lambda s: s.stop_event.set(), "останавливается"),
            "voice setup": (lambda s: setattr(s.owner_setup, "capturing", True), "эталона"),
            "hearing off": (lambda s: setattr(s.listening, "enabled", False), "не слушает"),
            "butler speaking": (lambda s: setattr(s.voice, "reply_in_progress", True), "говорит"),
            "muted": (lambda s: s.inputs.audio_status.update(input_muted=True), "выключен"),
            "zero gain": (lambda s: s.inputs.audio_status.update(input_volume=0), "ноль"),
            "no signal": (lambda s: setattr(s.inputs, "input_signal", None), "свежего звука"),
            "no hearing": (lambda s: setattr(s, "listening", None), "не настроен"),
        }
        for name, (break_it, word) in cases.items():
            with self.subTest(name):
                service = FakeService()
                break_it(service)
                dictation = Dictation(service)
                view = dictation.start()
                self.assertEqual(view["state"], "failed")
                self.assertIn(word, view["reason"])
                self.assertFalse(dictation.listening())
                self.assertFalse(dictation.hear("слова"))

    # -- hearing ----------------------------------------------------------------------------

    def test_phrases_accumulate_and_the_dictation_ends_two_seconds_after_the_last_words(self):
        self.dictation.start()
        self.service.advance(3)
        self.assertTrue(self.dictation.hear("Первая фраза."))
        self.service.advance(1.5)
        self.assertTrue(self.dictation.hear("Вторая фраза."))
        self.service.advance(SILENCE_AFTER - .1)
        self.assertEqual(self.dictation.snapshot()["state"], "listening")
        self.service.advance(.2)
        view = self.dictation.snapshot()
        self.assertEqual((view["state"], view["text"]), ("done", "Первая фраза. Вторая фраза."))

    def test_separate_phrases_are_not_merged_on_a_repeated_boundary_word(self):
        self.dictation.start()
        self.dictation.hear("Это да")
        self.dictation.hear("да конечно")
        self.assertEqual(self.dictation.snapshot()["text"], "Это да да конечно")

    def test_it_does_not_end_while_the_owner_is_still_speaking_or_being_recognised(self):
        self.dictation.start()
        self.dictation.hear("Начало")
        for speaking in ({"phase": "listening"}, {"phase": "transcription"}, {"processing": True}):
            with self.subTest(speaking):
                self.service.listening.__dict__.update(phase="waiting", processing=False)
                self.service.listening.__dict__.update(speaking)
                self.service.advance(SILENCE_AFTER + 5)
                self.assertEqual(self.dictation.snapshot()["state"], "listening")
        self.service.listening.__dict__.update(phase="waiting", processing=False)
        self.service.listening.jobs.put(("queued phrase",))
        self.service.advance(5)
        self.assertEqual(self.dictation.snapshot()["state"], "listening")           # an utterance waits for recognition
        self.service.listening.jobs.get()
        self.assertEqual(self.dictation.snapshot()["state"], "done")

    def test_nothing_heard_ends_neutrally_after_thirty_seconds(self):
        self.dictation.start()
        self.service.advance(NOTHING_HEARD_AFTER - 1)
        self.assertEqual(self.dictation.snapshot()["state"], "listening")
        self.service.advance(2)
        view = self.dictation.snapshot()
        self.assertEqual((view["state"], view["text"], view["note"], view["reason"]), ("done", "", NOTHING, None))

    def test_a_long_dictation_is_cut_at_the_limit_and_at_the_message_size(self):
        self.dictation.start()
        self.dictation.hear("слово")
        self.service.listening.phase = "listening"                                 # still talking
        self.service.advance(TOTAL_LIMIT + 1)
        self.assertEqual(self.dictation.snapshot()["state"], "done")
        self.dictation.start()
        self.dictation.start()
        self.dictation.hear("а" * (MAX_TEXT + 500))
        view = self.dictation.snapshot()
        self.assertEqual((view["state"], len(view["text"])), ("done", MAX_TEXT))

    def test_drafts_show_only_while_listening_and_never_survive_the_dictation(self):
        self.dictation.start()
        self.dictation.partial("  слышу вот это ")
        self.assertEqual(self.dictation.snapshot()["partial"], "слышу вот это")
        self.dictation.hear("слышу вот это")
        self.assertEqual(self.dictation.snapshot()["partial"], "")
        self.dictation.partial("ещё")
        self.service.advance(SILENCE_AFTER + 1)
        self.assertEqual(self.dictation.snapshot()["partial"], "")

    # -- stopping ---------------------------------------------------------------------------

    def test_stop_ends_at_once_when_nothing_is_being_recognised(self):
        self.dictation.start()
        self.dictation.hear("Готово")
        view = self.dictation.stop()
        self.assertEqual((view["state"], view["text"], view["stopping"]), ("done", "Готово", False))

    def test_stop_still_delivers_the_phrase_that_was_being_recognised(self):
        self.dictation.start()
        self.service.listening.processing = True                                   # the last sentence is in the recogniser
        view = self.dictation.stop()
        self.assertEqual((view["state"], view["stopping"]), ("listening", True))
        self.assertTrue(self.dictation.listening())                                 # it is still claimed
        self.dictation.hear("Последняя фраза")
        self.service.listening.processing = False
        view = self.dictation.snapshot()
        self.assertEqual((view["state"], view["text"]), ("done", "Последняя фраза"))

    def test_stop_does_not_wait_longer_than_the_grace_period(self):
        self.dictation.start()
        self.service.listening.phase = "listening"
        self.dictation.stop()
        self.service.advance(STOP_GRACE - 1)
        self.assertEqual(self.dictation.snapshot()["state"], "listening")
        self.service.advance(2)
        self.assertEqual(self.dictation.snapshot()["state"], "done")

    # -- interruption and expiry ------------------------------------------------------------

    def test_a_stopped_microphone_keeps_what_was_heard_or_reports_the_stop(self):
        self.dictation.start()
        self.dictation.hear("Уже сказано")
        self.dictation.interrupt()
        view = self.dictation.snapshot()
        self.assertEqual((view["state"], view["text"]), ("done", "Уже сказано"))
        self.assertIn("сохранено", view["note"])
        self.dictation.start()
        self.service.listening.enabled = False                                      # found out by the next poll
        view = self.dictation.snapshot()
        self.assertEqual(view["state"], "failed")
        self.assertIn("перестал слушать", view["reason"])

    def test_the_result_is_forgotten_after_two_minutes(self):
        self.dictation.start()
        self.dictation.hear("Забудется")
        self.dictation.stop()
        self.service.advance(KEEP_RESULT - 1)
        self.assertEqual(self.dictation.snapshot()["text"], "Забудется")
        self.service.advance(2)
        self.assertEqual(self.dictation.snapshot(), {"state": "idle", "id": None, "text": "", "partial": "", "stopping": False,
                                                     "reason": None, "note": None, "seconds": None})

    def test_dictated_words_are_never_logged(self):
        with self.assertNoLogs(level="DEBUG"):
            self.dictation.start()
            self.dictation.partial("личный черновик")
            self.dictation.hear("личная фраза")
            self.dictation.stop()
            self.dictation.snapshot()


class HearingPathTests(unittest.TestCase):
    """The real Listening: the first dictated sentence reaches the field and nothing else."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name))
        self.now = 100.
        self.inputs = Mock(audio_status={}, playback={"state": "idle"})
        self.s = Service(self.store, self.inputs, Mock(), Mock(), clock=lambda: self.now, transport=Mock())
        self.s.voice = Conversation(self.s)
        self.s.listening = Listening(self.s)
        self.l = self.s.listening
        self.l.model = Mock()
        self.s.voice.model = Mock()
        self.store.set("continuous_archive", True)
        self.store.set("face_wake_dialogue", True)
        self.store.set("voice_enabled", True)
        self.store.set("voice_profile", "baritone")
        self.s.gate.enable(True, self.now)
        self.inputs.audio_status = {"input_muted": False, "input_volume": 0.6}
        self.inputs.input_signal = {"at": self.now, "rms_dbfs": -30.0}
        self.s.feedback.transcript = Mock()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(self.store.close)
        self.addCleanup(self.s.stop)

    def process(self, text, owner=False, identifier=None, final=True):
        self.l.model.transcribe.return_value = text
        self.l.process(b"\0" * 32000, self.l.token, self.now, owner, time.monotonic(), False, identifier, final)

    def admit(self):
        for _ in range(3):
            self.now += 1
            self.s.dialogue_gate.check(Evidence(self.now, True, True, voice_ids=frozenset({"owner"}), camera_ids=frozenset({"owner"}),
                                                camera_certain=True, voice_certain=True, speech_detected=True), self.now, True)

    def start(self):
        self.inputs.input_signal = {"at": self.now, "rms_dbfs": -30.0}
        view = self.s.dictation.start()
        self.assertEqual(view["state"], "listening", view)

    def test_an_addressed_first_sentence_reaches_the_field_and_not_the_voice_dialogue(self):
        self.admit()
        self.start()
        self.process("Батлер, напиши Тане, что я задержусь.", owner=True)
        self.assertTrue(self.s.voice.jobs.empty())                                  # the voice dialogue never saw it
        self.assertEqual(self.s.dictation.snapshot()["text"], "Батлер, напиши Тане, что я задержусь.")
        self.assertEqual(self.l.last_heard, "")                                      # not shown as heard
        self.assertEqual(self.l.phase, "waiting")

    def test_the_words_are_kept_nowhere_in_the_hearing_state_or_feedback(self):
        self.admit()
        self.start()
        words = "Секретная диктовка про отпуск"
        self.process(words, owner=True)
        text = json.dumps([list(self.l.events), self.l.snapshot(), self.s.feedback.rows], default=str, ensure_ascii=False)
        self.assertNotIn(words, text)
        self.assertEqual(len(self.s.feedback.rows), 0)                               # no episode was captured
        for call in self.s.feedback.transcript.call_args_list:
            self.assertEqual(call.args[0]["text"], "")
        self.assertEqual(self.l.counts.get("dictated"), 1)

    def test_drafts_of_a_dictated_utterance_go_to_the_status_line_only(self):
        self.admit()
        self.start()
        identifier = self.l._begin(self.now - 2)
        self.l._draft(identifier, "черновик диктовки")
        self.assertEqual(self.s.dictation.snapshot()["partial"], "черновик диктовки")
        self.assertEqual(self.l.snapshot()["transcript"]["text"], "")
        self.s.feedback.transcript.assert_not_called()

    def test_an_unfinished_sentence_is_delivered_without_waiting_for_a_continuation(self):
        self.admit()
        self.start()
        self.process("Я хочу чтобы ты", owner=True)
        self.assertEqual(self.s.dictation.snapshot()["text"], "Я хочу чтобы ты")
        self.assertIsNone(self.l.pending_thought)
        self.assertNotEqual(self.l.phase, "awaiting-continuation")

    def test_smart_turn_is_not_consulted_while_dictating(self):
        self.admit()
        self.s.smart_turn = Mock()
        self.s.smart_turn.complete.return_value = False                              # «thought not finished»
        self.start()
        self.process("Продиктованная мысль", owner=True)
        self.s.smart_turn.complete.assert_not_called()
        self.assertEqual(self.s.dictation.snapshot()["text"], "Продиктованная мысль")

    def test_long_speech_is_joined_across_segments_and_delivered_once(self):
        self.admit()
        self.start()
        identifier = self.l._begin(self.now - 2)
        self.process("начало длинной мысли", owner=True, identifier=identifier, final=False)
        self.assertEqual(self.s.dictation.snapshot()["text"], "")                    # a segment is not an utterance
        self.process("мысли и её конец", owner=True, identifier=identifier, final=True)
        self.assertEqual(self.s.dictation.snapshot()["text"], "начало длинной мысли и её конец")

    def test_without_dictation_the_same_words_are_heard_and_routed_as_before(self):
        self.admit()
        self.process("Батлер, напиши Тане, что я задержусь.", owner=True)
        self.assertEqual(self.l.last_heard, "Батлер, напиши Тане, что я задержусь.")
        self.assertEqual(self.s.voice.jobs.qsize(), 1)
        self.assertEqual(self.s.dictation.snapshot()["state"], "idle")

    def test_after_stop_the_next_utterance_is_a_normal_one_again(self):
        self.admit()
        self.start()
        self.process("Первая", owner=True)
        self.s.dictation.stop()
        self.assertEqual(self.s.dictation.snapshot()["state"], "done")
        self.now += 1
        self.process("Батлер, вторая", owner=True)
        self.assertEqual(self.s.voice.jobs.qsize(), 1)
        self.assertEqual(self.s.dictation.snapshot()["text"], "Первая")

    def test_pausing_the_microphone_interrupts_and_keeps_what_was_dictated(self):
        self.admit()
        self.start()
        self.process("Уже продиктовано", owner=True)
        self.l.clear()
        view = self.s.dictation.snapshot()
        self.assertEqual((view["state"], view["text"]), ("done", "Уже продиктовано"))
        self.assertIn("перестал слушать", view["note"])

    def test_speech_that_recognises_to_nothing_adds_no_text(self):
        self.admit()
        self.start()
        self.process("", owner=True)
        self.assertEqual(self.s.dictation.snapshot()["text"], "")
        self.assertEqual(self.l.counts.get("asr-empty"), 1)


class CommandTests(unittest.TestCase):
    def run_command(self, args, dictation):
        instance = Handler.__new__(Handler)
        instance.connection = Mock()
        instance.rfile = io.BytesIO(json.dumps({"command": "ui-dictate", "args": args}).encode() + b"\n")
        instance.wfile = io.BytesIO()
        instance.server = SimpleNamespace(service=SimpleNamespace(dictation=dictation))
        instance.handle()
        return json.loads(instance.wfile.getvalue())

    def test_start_and_stop_reach_the_dictation(self):
        dictation = Mock()
        dictation.start.return_value = {"state": "listening"}
        dictation.stop.return_value = {"state": "done"}
        self.assertEqual(self.run_command({"action": "start"}, dictation), {"result": {"state": "listening"}})
        self.assertEqual(self.run_command({"action": "stop"}, dictation), {"result": {"state": "done"}})

    def test_anything_else_is_refused(self):
        dictation = Mock()
        for args in ({}, {"action": "pause"}, {"action": "start", "x": 1}, {"act": "start"}):
            self.assertIn("error", self.run_command(args, dictation))
        dictation.start.assert_not_called()
        dictation.stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
