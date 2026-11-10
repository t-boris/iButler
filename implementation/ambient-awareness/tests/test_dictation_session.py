"""Dictation as its own microphone session while observation is paused (DEC-049).

Pause stops every background source. The owner's «Диктовать» button is an explicit, bounded action: it opens only the
microphone, returns text to the panel's message field and keeps nothing.
"""
import queue
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from butler_ambient.dictation import (NOTHING, OWN_IDLE_LIMIT, OWN_TOTAL_LIMIT, PANEL_GONE_AFTER, STOP_GRACE, Dictation)
from butler_ambient.dictation_capture import DictationCapture
from butler_ambient.panel_chat import MAX_TEXT
from butler_ambient.sensing import MicrophoneInputs


def wait_for(predicate, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class PausedService:
    """Just what Dictation reads while observation is paused."""

    def __init__(self):
        self.now = 1000.0
        self.lock = threading.RLock()
        self.gate = SimpleNamespace(enabled=False)
        self.stop_event = threading.Event()
        self.owner_setup = SimpleNamespace(capturing=False)
        self.voice = SimpleNamespace(reply_in_progress=False, state="ready", phase="idle")
        self.listening = SimpleNamespace(enabled=False, phase="paused", processing=False, jobs=queue.Queue(), audio=bytearray(),
                                         model=Mock(), begin_dictation=Mock(), stop_dictation=Mock())
        self.inputs = SimpleNamespace()

    def clock(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class FakeCapture:
    def __init__(self, fail_start=None):
        self.fail_start = fail_start
        self.started, self.stop_requests, self.aborts = False, 0, 0

    def start(self):
        if self.fail_start:
            raise self.fail_start
        self.started = True

    def request_stop(self):
        self.stop_requests += 1

    def abort(self):
        self.aborts += 1


class OwnSessionTests(unittest.TestCase):
    def setUp(self):
        self.service = PausedService()
        self.dictation = Dictation(self.service)
        self.capture = FakeCapture()
        self.dictation.capture_factory = lambda service, dictation: self.capture

    def poll(self, seconds, step=5):
        """The panel polls about once a second; a step of five seconds is enough to keep it alive."""
        end = self.service.now + seconds
        while self.service.now < end:
            self.service.advance(min(step, end - self.service.now))
            self.dictation.snapshot()

    # -- starting ---------------------------------------------------------------------------

    def test_start_during_pause_opens_the_own_session_and_leaves_the_hearing_path_alone(self):
        view = self.dictation.start()
        self.assertEqual((view["state"], view["text"]), ("listening", ""))
        self.assertTrue(self.capture.started)
        self.assertFalse(self.dictation.listening())                        # the hearing path must never claim its words
        self.service.listening.begin_dictation.assert_not_called()
        self.assertEqual(self.dictation.start()["id"], view["id"])           # a second press is the same dictation

    def test_every_reason_it_cannot_start_is_stated_and_no_microphone_is_opened(self):
        cases = {
            "stopping": (lambda s: s.stop_event.set(), "останавливается"),
            "voice setup": (lambda s: setattr(s.owner_setup, "capturing", True), "эталона"),
            "butler speaking": (lambda s: setattr(s.voice, "state", "speaking"), "говорит"),
            "no hearing": (lambda s: setattr(s, "listening", None), "не настроен"),
            "no recogniser": (lambda s: setattr(s.listening, "model", None), "не настроен"),
        }
        for name, (break_it, word) in cases.items():
            with self.subTest(name):
                service, capture = PausedService(), FakeCapture()
                break_it(service)
                dictation = Dictation(service)
                dictation.capture_factory = lambda service, dictation, capture=capture: capture
                view = dictation.start()
                self.assertEqual(view["state"], "failed")
                self.assertIn(word, view["reason"])
                self.assertFalse(capture.started)
                self.assertFalse(dictation.hear("слова", session=capture))

    def test_a_microphone_that_cannot_be_opened_is_reported_and_nothing_stays_claimed(self):
        self.capture.fail_start = OSError("no such device")
        view = self.dictation.start()
        self.assertEqual(view["state"], "failed")
        self.assertIn("Не удалось открыть микрофон", view["reason"])
        self.assertNotIn("no such device", view["reason"])                  # no system detail in the owner's words
        self.assertFalse(self.dictation.hear("слова", session=self.capture))
        self.assertEqual(self.dictation.start()["state"], "failed")          # and a retry is possible

    # -- words ------------------------------------------------------------------------------

    def test_only_the_current_session_can_write_words(self):
        self.dictation.start()
        other = FakeCapture()
        self.assertFalse(self.dictation.hear("чужое", session=other))
        self.assertFalse(self.dictation.hear("из слуха"))                    # nor the hearing path, which is not running
        self.assertTrue(self.dictation.hear("своё", session=self.capture))
        self.assertTrue(self.dictation.hear("и ещё", session=self.capture))
        self.assertEqual(self.dictation.snapshot()["text"], "своё и ещё")

    def test_an_old_session_cannot_write_into_a_new_dictation(self):
        self.dictation.start()
        self.dictation.stop()
        self.dictation.session_finished(self.capture)
        old, self.capture = self.capture, FakeCapture()
        self.dictation.start()
        self.assertFalse(self.dictation.hear("старое", session=old))
        self.assertFalse(self.dictation.session_tick(old))
        self.assertEqual(self.dictation.snapshot()["text"], "")

    def test_the_text_limit_ends_the_session_by_itself(self):
        self.dictation.start()
        self.dictation.hear("а" * (MAX_TEXT + 50), session=self.capture)
        self.assertEqual(len(self.dictation.snapshot()["text"]), MAX_TEXT)
        self.assertEqual(self.capture.stop_requests, 1)

    # -- stopping ---------------------------------------------------------------------------

    def test_stop_asks_the_session_for_the_last_words_and_finishes_when_it_reports(self):
        self.dictation.start()
        view = self.dictation.stop()
        self.assertEqual((view["state"], view["stopping"]), ("listening", True))
        self.assertEqual(self.capture.stop_requests, 1)
        self.dictation.stop()
        self.assertEqual(self.capture.stop_requests, 1)                      # asked once
        self.assertTrue(self.dictation.hear("Последняя фраза", session=self.capture))
        self.dictation.session_finished(self.capture)
        view = self.dictation.snapshot()
        self.assertEqual((view["state"], view["text"], view["stopping"]), ("done", "Последняя фраза", False))

    def test_stop_with_nothing_said_says_so(self):
        self.dictation.start()
        self.dictation.stop()
        self.dictation.session_finished(self.capture)
        view = self.dictation.snapshot()
        self.assertEqual((view["state"], view["text"], view["note"]), ("done", "", NOTHING))

    def test_stop_does_not_wait_longer_than_the_grace_period(self):
        self.dictation.start()
        self.dictation.hear("Уже есть", session=self.capture)
        self.dictation.stop()
        self.poll(STOP_GRACE - 5)
        self.assertEqual(self.dictation.snapshot()["state"], "listening")
        self.poll(10)
        view = self.dictation.snapshot()
        self.assertEqual((view["state"], view["text"]), ("done", "Уже есть"))
        self.assertEqual(self.capture.aborts, 1)                             # the microphone is closed for certain

    # -- safeguards: the microphone never stays open by accident ----------------------------

    def test_two_minutes_without_recognised_words_close_the_microphone(self):
        self.dictation.start()
        self.poll(OWN_IDLE_LIMIT - 10)
        self.assertEqual(self.capture.stop_requests, 0)
        self.poll(15)
        self.assertEqual(self.capture.stop_requests, 1)
        self.dictation.session_finished(self.capture)
        view = self.dictation.snapshot()
        self.assertEqual(view["state"], "done")
        self.assertIn("тишины", view["note"])

    def test_recognised_words_restart_the_silence_clock(self):
        self.dictation.start()
        self.poll(OWN_IDLE_LIMIT - 10)
        self.dictation.hear("Ещё говорю", session=self.capture)
        self.poll(OWN_IDLE_LIMIT - 10)
        self.assertEqual(self.capture.stop_requests, 0)

    def test_a_panel_that_stops_polling_closes_the_microphone_at_once(self):
        self.dictation.start()
        self.dictation.hear("Что успел", session=self.capture)
        self.service.advance(PANEL_GONE_AFTER - 1)
        self.assertTrue(self.dictation.session_tick(self.capture))           # the worker's own check, without a poll
        self.service.advance(2)
        self.assertFalse(self.dictation.session_tick(self.capture))
        self.assertEqual(self.capture.aborts, 1)
        view = self.dictation.snapshot()
        self.assertEqual((view["state"], view["text"]), ("done", "Что успел"))
        self.assertIn("Панель", view["note"])

    def test_a_panel_that_vanishes_before_any_words_leaves_a_failed_dictation(self):
        self.dictation.start()
        self.service.advance(PANEL_GONE_AFTER + 1)
        self.assertFalse(self.dictation.session_tick(self.capture))
        self.assertEqual(self.dictation.snapshot()["state"], "failed")
        self.assertEqual(self.capture.aborts, 1)

    def test_twenty_minutes_in_total_end_even_a_talkative_dictation(self):
        self.dictation.start()
        for _ in range(int(OWN_TOTAL_LIMIT // 60) + 1):
            self.poll(60)
            self.dictation.hear("и дальше", session=self.capture)
            if self.capture.stop_requests:
                break
        self.assertEqual(self.capture.stop_requests, 1)
        self.assertGreaterEqual(self.service.now - 1000.0, OWN_TOTAL_LIMIT)
        self.assertLess(self.service.now - 1000.0, OWN_TOTAL_LIMIT + 61)

    def test_resuming_observation_ends_the_own_session_and_keeps_the_words(self):
        self.dictation.start()
        self.dictation.hear("Продиктовано", session=self.capture)
        self.service.gate.enabled = True                                      # the owner pressed «Продолжить»
        view = self.dictation.snapshot()
        self.assertEqual((view["state"], view["text"]), ("done", "Продиктовано"))
        self.assertEqual(self.capture.aborts, 1)
        self.assertIn("сохранено", view["note"])

    def test_the_service_stopping_closes_the_microphone(self):
        self.dictation.start()
        self.service.stop_event.set()
        self.assertFalse(self.dictation.session_tick(self.capture))
        self.assertEqual(self.capture.aborts, 1)
        self.assertEqual(self.dictation.snapshot()["state"], "failed")

    def test_an_owner_voice_setup_taking_the_microphone_ends_the_session(self):
        self.dictation.start()
        self.service.owner_setup.capturing = True
        self.assertFalse(self.dictation.session_tick(self.capture))
        self.assertEqual(self.capture.aborts, 1)

    def test_clearing_the_hearing_path_interrupts_the_session_and_keeps_the_words(self):
        self.dictation.start()
        self.dictation.hear("Сказано", session=self.capture)
        self.dictation.interrupt("hearing-stopped")                          # what Listening.clear() calls
        view = self.dictation.snapshot()
        self.assertEqual((view["state"], view["text"]), ("done", "Сказано"))
        self.assertEqual(self.capture.aborts, 1)

    # -- failures reported by the session ---------------------------------------------------

    def test_a_session_failure_is_stated_to_the_owner(self):
        self.dictation.start()
        self.dictation.session_failed(self.capture, "Микрофон выключен на устройстве или в системе.")
        view = self.dictation.snapshot()
        self.assertEqual((view["state"], view["reason"]), ("failed", "Микрофон выключен на устройстве или в системе."))
        self.assertEqual(self.capture.aborts, 1)

    def test_a_failure_after_some_words_keeps_them(self):
        self.dictation.start()
        self.dictation.hear("Успели", session=self.capture)
        self.dictation.session_failed(self.capture, "Распознавание речи недоступно.")
        view = self.dictation.snapshot()
        self.assertEqual((view["state"], view["text"]), ("done", "Успели"))
        self.assertIn("Распознавание речи недоступно", view["note"])

    def test_the_status_line_shows_the_stage_only_while_listening(self):
        self.dictation.start()
        self.dictation.partial("Слушаю…")
        self.assertEqual(self.dictation.snapshot()["partial"], "Слушаю…")
        self.dictation.stop()
        self.dictation.session_finished(self.capture)
        self.assertEqual(self.dictation.snapshot()["partial"], "")

    def test_dictated_words_are_never_logged(self):
        with self.assertNoLogs(level="DEBUG"):
            self.dictation.start()
            self.dictation.partial("личный черновик")
            self.dictation.hear("личная фраза", session=self.capture)
            self.dictation.stop()
            self.dictation.session_finished(self.capture)
            self.dictation.snapshot()

    # -- observation on: the hearing path is used exactly as before -------------------------

    def test_with_observation_on_dictation_still_rides_the_hearing_path(self):
        self.service.gate.enabled = True
        self.service.listening.enabled = True
        self.service.inputs = SimpleNamespace(audio_status={"input_muted": False, "input_volume": 0.6},
                                              input_signal={"at": self.service.now, "rms_dbfs": -30.0})
        view = self.dictation.start()
        self.assertEqual(view["state"], "listening")
        self.assertFalse(self.capture.started)                                # no second microphone
        self.assertTrue(self.dictation.listening())
        self.service.listening.begin_dictation.assert_called_once()


# -- the capture worker ---------------------------------------------------------------------

LOUD = b"\x01\x00" * 16000                         # one second the test treats as speech
QUIET = b"\x00\x00" * 16000
ENDING = b"\x01\x00" * 8000 + b"\x00\x00" * 8000   # speech, then half a second of quiet


class FakeMicrophone:
    """What DictationCapture reads: whole-second blocks, the helper's status, and a close() that ends the stream."""

    def __init__(self):
        self.audio = queue.Queue()
        self.audio_status = {"ready": True, "input_muted": False, "input_volume": 0.6}
        self.input_signal = None
        self.closing, self.ended = threading.Event(), threading.Event()
        self.closed_at = None
        self.started = False
        self.closes = []

    def start(self):
        self.started = True

    def put(self, pcm):
        self.audio.put((time.time(), pcm, "stream", 0))

    def close(self, flush=False):
        self.closes.append(flush)
        if not self.closing.is_set():
            self.closing.set()
            self.closed_at = time.time()
        self.ended.set()


class MarkerCapture(DictationCapture):
    """Voice activity from the test's own markers instead of WebRTC."""
    FIRST_SOUND_WITHIN = 0.4

    @staticmethod
    def speech(pcm):
        return pcm[:2] == b"\x01\x00"

    @staticmethod
    def activity(pcm):
        return b"\x01\x00" in pcm

    @staticmethod
    def quiet_tail(pcm):
        return 0.5 if pcm[-2:] == b"\x00\x00" else 0.0


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.service = PausedService()
        self.model = self.service.listening.model
        self.model.transcribe.return_value = "привет мир"
        self.microphone = FakeMicrophone()
        self.dictation = Dictation(self.service)
        self.dictation.capture_factory = lambda service, dictation: MarkerCapture(service, dictation, self.microphone)
        self.addCleanup(self.finish)

    def finish(self):
        self.dictation.interrupt()
        capture = self.dictation.capture
        if capture is not None and capture.thread is not None:
            capture.thread.join(3)

    def start(self):
        view = self.dictation.start()
        self.assertEqual(view["state"], "listening", view)
        self.assertTrue(self.microphone.started)
        return self.dictation.capture

    def state(self):
        return self.dictation.snapshot()

    def test_a_phrase_is_recognised_when_the_speaker_pauses(self):
        self.start()
        for block in (QUIET, LOUD, LOUD, ENDING):
            self.microphone.put(block)
        self.assertTrue(wait_for(lambda: self.state()["text"] == "привет мир"))
        self.assertEqual(self.model.transcribe.call_count, 1)
        wav, kwargs = self.model.transcribe.call_args.args[0], self.model.transcribe.call_args.kwargs
        self.assertEqual(wav[:4], b"RIFF")
        self.assertIn("token", kwargs)
        self.assertEqual(len(wav), 44 + 4 * 32000)                             # pre-roll second + three blocks, nothing else
        self.assertEqual(self.state()["state"], "listening")                   # the session goes on until «Остановить»

    def test_a_second_phrase_is_added_after_the_first(self):
        self.start()
        self.model.transcribe.side_effect = ["первая.", "вторая."]
        for block in (LOUD, ENDING, QUIET, LOUD, ENDING):
            self.microphone.put(block)
        self.assertTrue(wait_for(lambda: self.state()["text"] == "первая. вторая."))

    def test_silence_alone_never_reaches_the_recogniser(self):
        self.start()
        for _ in range(6):
            self.microphone.put(QUIET)
        self.assertTrue(wait_for(lambda: self.microphone.audio.empty()))
        time.sleep(0.15)
        self.model.transcribe.assert_not_called()
        self.assertEqual(self.state()["state"], "listening")

    def test_the_status_line_says_when_the_microphone_is_ready(self):
        self.start()
        self.assertEqual(self.state()["partial"], "Включаю микрофон…")
        self.microphone.put(QUIET)
        self.assertTrue(wait_for(lambda: self.state()["partial"] == "Слушаю…"))

    def test_stop_delivers_the_unfinished_phrase_and_closes_the_microphone(self):
        capture = self.start()
        self.model.transcribe.return_value = "хвост фразы"
        self.microphone.put(LOUD)
        self.microphone.put(LOUD)                                              # still speaking when «Остановить» is pressed
        self.assertTrue(wait_for(lambda: self.microphone.audio.empty()))
        view = self.dictation.stop()
        self.assertTrue(view["stopping"])
        self.assertTrue(wait_for(lambda: self.state()["state"] == "done"))
        self.assertEqual(self.state()["text"], "хвост фразы")
        self.assertEqual(self.microphone.closes, [True])                       # closed with the last partial second kept
        capture.thread.join(3)
        self.assertFalse(capture.thread.is_alive())

    def test_stop_without_speech_ends_quickly_with_the_nothing_heard_note(self):
        self.start()
        self.microphone.put(QUIET)
        self.dictation.stop()
        self.assertTrue(wait_for(lambda: self.state()["state"] == "done"))
        self.assertEqual((self.state()["text"], self.state()["note"]), ("", NOTHING))
        self.model.transcribe.assert_not_called()

    def test_endless_speech_is_cut_into_segments_before_the_recogniser_limit(self):
        self.start()
        for _ in range(26):
            self.microphone.put(LOUD)
        self.assertTrue(wait_for(lambda: self.model.transcribe.call_count == 1))
        wav = self.model.transcribe.call_args.args[0]
        self.assertLessEqual(len(wav) - 44, 25 * 32000)

    def test_recognition_that_returns_nothing_adds_no_text(self):
        self.start()
        self.model.transcribe.return_value = ""                                # a filtered phantom or low confidence
        for block in (LOUD, ENDING):
            self.microphone.put(block)
        self.assertTrue(wait_for(lambda: self.model.transcribe.call_count == 1))
        time.sleep(0.1)
        self.assertEqual((self.state()["text"], self.state()["state"]), ("", "listening"))

    def test_a_muted_microphone_fails_with_the_reason(self):
        self.microphone.audio_status["input_muted"] = True
        self.start()
        self.microphone.put(QUIET)
        self.assertTrue(wait_for(lambda: self.state()["state"] == "failed"))
        self.assertIn("выключен", self.state()["reason"])
        self.assertEqual(self.microphone.closes, [False])

    def test_zero_input_gain_fails_with_the_reason(self):
        self.microphone.audio_status["input_volume"] = 0
        self.start()
        self.microphone.put(QUIET)
        self.assertTrue(wait_for(lambda: self.state()["state"] == "failed"))
        self.assertIn("ноль", self.state()["reason"])

    def test_no_sound_at_all_fails_with_the_reason(self):
        self.start()                                                           # FIRST_SOUND_WITHIN is 0.4 s here
        self.assertTrue(wait_for(lambda: self.state()["state"] == "failed"))
        self.assertIn("свежего звука", self.state()["reason"])
        self.assertEqual(self.microphone.closes, [False])

    def test_one_failed_recognition_drops_that_phrase_only(self):
        self.start()
        self.model.transcribe.side_effect = [ValueError("ASR failed"), "вторая фраза"]
        for block in (LOUD, ENDING, QUIET, LOUD, ENDING):
            self.microphone.put(block)
        self.assertTrue(wait_for(lambda: self.state()["text"] == "вторая фраза"))
        self.assertEqual(self.state()["state"], "listening")

    def test_repeated_recognition_failures_fail_the_dictation(self):
        self.start()
        self.model.transcribe.side_effect = ValueError("ASR failed")
        for _ in range(3):
            for block in (LOUD, ENDING, QUIET):
                self.microphone.put(block)
        self.assertTrue(wait_for(lambda: self.state()["state"] == "failed"))
        self.assertIn("Распознавание речи недоступно", self.state()["reason"])

    def test_abort_cancels_a_recognition_in_flight_and_delivers_nothing(self):
        entered = threading.Event()

        def transcribe(wav, token=None):
            entered.set()
            token.wait(5)
            raise RuntimeError("ASR cancelled")

        self.model.transcribe.side_effect = transcribe
        capture = self.start()
        for block in (LOUD, ENDING):
            self.microphone.put(block)
        self.assertTrue(entered.wait(3))
        self.dictation.interrupt("hearing-stopped")
        capture.thread.join(3)
        self.assertFalse(capture.thread.is_alive())
        self.assertEqual(self.microphone.closes, [False])
        self.assertEqual(self.state()["text"], "")

    def test_no_audio_is_kept_once_a_phrase_is_delivered_or_the_session_ends(self):
        capture = self.start()
        for block in (LOUD, ENDING):
            self.microphone.put(block)
        self.assertTrue(wait_for(lambda: self.state()["text"] == "привет мир"))
        self.assertEqual(capture.buffered, 0)
        self.microphone.put(LOUD)
        self.assertTrue(wait_for(lambda: capture.buffered > 0))
        self.dictation.interrupt()
        capture.thread.join(3)
        self.assertEqual(capture.buffered, 0)

    def test_the_words_are_not_logged_by_the_capture(self):
        with self.assertNoLogs(level="DEBUG"):
            self.start()
            for block in (LOUD, ENDING):
                self.microphone.put(block)
            self.assertTrue(wait_for(lambda: self.state()["text"] == "привет мир"))
            self.dictation.stop()
            self.assertTrue(wait_for(lambda: self.state()["state"] == "done"))


# -- the microphone-only input ---------------------------------------------------------------

STUB_HELPER = """
import json, sys, time
sys.stderr.write("BUTLER_AUDIO " + json.dumps({"state": "ready", "echo_cancellation": False}) + "\\n")
sys.stderr.flush()
sys.stdout.buffer.write(b"\\x01\\x00" * 24000)        # one whole second and half a second more
sys.stdout.buffer.flush()
sys.stderr.write("BUTLER_AUDIO " + json.dumps({"state": "health", "input_volume": 0.5}) + "\\n")   # all audio is written
sys.stderr.flush()
time.sleep(30)
"""


class MicrophoneInputsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        helper = Path(self.temp.name) / "butler-audio"
        helper.write_text(f"#!{sys.executable}\n" + textwrap.dedent(STUB_HELPER))
        helper.chmod(0o755)
        self.helper = helper
        self.inputs = MicrophoneInputs(audio_helper=helper, voice_root=self.temp.name, expected_microphone="Test Microphone")
        self.addCleanup(self.inputs.close)

    def test_only_the_microphone_helper_is_started(self):
        self.inputs.start()
        started = [process for process in self.inputs.processes if process is not None]
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0].args[0], str(self.helper))
        self.assertIsNone(self.inputs.processes[0])                            # the camera slot stays empty
        self.assertIsNone(self.inputs.camera_started_at)

    def test_audio_arrives_in_whole_seconds_and_the_helper_status_is_read(self):
        self.inputs.start()
        _at, pcm, _stream, _start = self.inputs.audio.get(timeout=3)
        self.assertEqual(pcm, b"\x01\x00" * 16000)
        self.assertTrue(wait_for(lambda: self.inputs.audio_status.get("ready") is True))

    def test_closing_with_flush_delivers_the_last_partial_second_padded_with_silence(self):
        self.inputs.start()
        self.inputs.audio.get(timeout=3)
        self.assertTrue(wait_for(lambda: self.inputs.audio_status.get("input_volume") == 0.5))     # the helper wrote it all
        self.inputs.close(flush=True)
        _at, pcm, _stream, _start = self.inputs.audio.get(timeout=3)
        self.assertEqual(len(pcm), 32000)
        self.assertEqual(pcm[:16000], b"\x01\x00" * 8000)
        self.assertEqual(pcm[16000:], b"\x00" * 16000)
        self.assertTrue(self.inputs.ended.is_set())

    def test_closing_without_flush_drops_the_partial_second(self):
        self.inputs.start()
        self.inputs.audio.get(timeout=3)
        self.assertTrue(wait_for(lambda: self.inputs.audio_status.get("input_volume") == 0.5))
        self.inputs.close(flush=False)
        with self.assertRaises(queue.Empty):
            self.inputs.audio.get(timeout=0.3)

    def test_close_ends_the_helper_and_is_safe_to_repeat(self):
        self.inputs.start()
        process = next(process for process in self.inputs.processes if process is not None)
        self.inputs.close()
        self.assertIsNotNone(process.poll())
        self.inputs.close()
        self.inputs.close(flush=True)
        self.assertEqual(self.inputs.processes, [])

    def test_a_closed_microphone_can_never_be_reopened_by_the_recovery_paths(self):
        # A helper that dies on its own just as «Остановить» is pressed must not be restarted behind the owner's back.
        self.inputs.start()
        self.inputs.audio.get(timeout=3)
        self.inputs.close()
        self.assertFalse(self.inputs.recover_audio(force=True))
        self.assertEqual(self.inputs.processes, [])
        self.inputs._fallback_audio(Mock())                                    # refuses before it would start FFmpeg
        self.assertEqual(self.inputs.processes, [])

    def test_closing_waits_for_a_recovery_that_is_already_replacing_the_helper(self):
        self.inputs.start()
        self.inputs.audio.get(timeout=3)
        self.inputs.control_lock.acquire()                                     # what recover_audio() holds while it swaps helpers
        closer = threading.Thread(target=self.inputs.close, daemon=True)
        closer.start()
        time.sleep(0.3)
        self.assertTrue(closer.is_alive())                                     # it must see the helper that recovery starts
        self.assertTrue(self.inputs.stopped.is_set())                          # while any later recovery already refuses
        self.inputs.control_lock.release()
        closer.join(5)
        self.assertFalse(closer.is_alive())
        self.assertEqual(self.inputs.processes, [])

    def test_a_helper_that_cannot_start_is_reported(self):
        broken = MicrophoneInputs(audio_helper=Path(self.temp.name) / "missing", voice_root=self.temp.name)
        with self.assertRaises(RuntimeError):
            broken.start()
        broken.close()

    def test_it_can_be_started_again_after_closing(self):
        self.inputs.start()
        self.inputs.audio.get(timeout=3)
        self.inputs.close()
        self.inputs.start()
        _at, pcm, _stream, _start = self.inputs.audio.get(timeout=3)
        self.assertEqual(len(pcm), 32000)


if __name__ == "__main__":
    unittest.main()
