"""The microphone session behind «Диктовать» while observation is paused (DEC-049).

Pause stops every background source. The owner's button is an explicit, bounded action, so it opens only the
microphone (no camera, no screen, no identity checks), cuts the speech into phrases at the quiet moments, has the
resident local Whisper recognise each phrase (its phantom and confidence filters stay inside it) and hands the text to
``Dictation``, which gives it to the panel's message field and nowhere else. Audio exists only in memory here, for the
phrase being spoken; nothing is stored, logged or sent. The session ends with «Остановить» (the last words are still
recognised) or through the safeguards that ``Dictation`` enforces on every heartbeat of this worker.
"""
from __future__ import annotations

import queue
import threading
import time

from .sensing import Batch, MicrophoneInputs

BLOCK_BYTES = 32000                 # one second of 16 kHz mono 16-bit PCM
QUIET_TAIL = .45                    # seconds of quiet that end a phrase (the same as in the hearing path)


def open_capture(service, dictation):
    """A capture on the microphone the observation service is configured with."""
    inputs = service.inputs
    microphone = MicrophoneInputs(
        camera=getattr(inputs, "camera", "0"), microphone=getattr(inputs, "microphone", "0"),
        audio_helper=getattr(inputs, "audio_helper", None), voice_root=getattr(inputs, "voice_root", None),
        expected_microphone=getattr(inputs, "expected_microphone", "Jabra SPEAK"),
        ffmpeg_cli=getattr(inputs, "ffmpeg_cli", "ffmpeg"))
    return DictationCapture(service, dictation, microphone)


class DictationCapture:
    FIRST_SOUND_WITHIN = 8.0        # seconds after the press before «no sound from the microphone» is declared
    SEGMENT_SECONDS = 25            # longer speech without a pause is recognised in pieces
    FAILURES_ALLOWED = 3            # consecutive failed recognitions before the dictation is declared unavailable

    def __init__(self, service, dictation, microphone):
        self.service, self.dictation, self.microphone = service, dictation, microphone
        self.token = threading.Event()          # cancels a recognition in flight
        self.aborted = threading.Event()
        self.thread = None
        self.started_at = None
        self.audio = bytearray()                # the phrase being spoken
        self.previous = b""                     # the second before it, so that the first syllable is not lost
        self.failures = 0

    @property
    def buffered(self):
        return len(self.audio) + len(self.previous)

    # -- voice activity (the same detectors as the hearing path; replaceable in tests) ------------------------------

    @staticmethod
    def speech(pcm):
        from .listening import Listening
        return Listening.speech(pcm)

    @staticmethod
    def activity(pcm):
        from .listening import Listening
        return Listening.confirmation_candidate(pcm)

    @staticmethod
    def quiet_tail(pcm):
        from .conversation import Conversation
        return Conversation._silence_tail(pcm)

    # -- the commands Dictation gives ---------------------------------------------------------------------------------

    def start(self):
        self.started_at = time.monotonic()
        self.microphone.start()
        self.thread = threading.Thread(target=self._run, name="dictation-microphone", daemon=True)
        self.thread.start()

    def request_stop(self):
        """«Остановить»: close the microphone now, keeping the last partial second; the worker recognises what is left."""
        self.microphone.close(flush=True)

    def abort(self):
        """End at once: cancel a recognition in flight and close the microphone; nothing more is delivered."""
        self.aborted.set()
        self.token.set()
        self.microphone.close(flush=False)

    # -- the worker -----------------------------------------------------------------------------------------------------

    def _run(self):
        first_block = False
        try:
            while True:
                if not self.dictation.session_tick(self):
                    return                       # Dictation ended it and has closed the microphone
                try:
                    block = self.microphone.audio.get(timeout=.2)
                except queue.Empty:
                    if self._stream_ended():
                        break
                    if not first_block and time.monotonic() - self.started_at > self.FIRST_SOUND_WITHIN:
                        self._fail("С микрофона нет свежего звука: проверьте, что он подключён и не занят другим приложением.")
                        return
                    continue
                if not first_block:
                    first_block = True
                    self.dictation.partial("Слушаю…")
                problem = self._microphone_problem()
                if problem:
                    self._fail(problem)
                    return
                self._take(block[1])
            # The microphone was closed by «Остановить»: the phrase that was being spoken is still recognised.
            if self.audio and not self.aborted.is_set():
                self._recognise()
            self.dictation.session_finished(self)
        except Exception:                        # never leave the microphone open on a bug; nothing about the audio is logged
            self._fail("Диктовка остановилась из-за внутренней ошибки.")
        finally:
            self.audio = bytearray()
            self.previous = b""
            if not self.microphone.closing.is_set():
                self.microphone.close(flush=False)

    def _stream_ended(self):
        microphone = self.microphone
        if not microphone.closing.is_set():
            return False
        if microphone.ended.is_set():
            return microphone.audio.empty()      # everything the reader produced has been taken
        return microphone.closed_at is not None and time.time() - microphone.closed_at > 3

    def _microphone_problem(self):
        status = self.microphone.audio_status
        status = status if isinstance(status, dict) else {}
        if status.get("input_muted") is True:
            return "Микрофон выключен на устройстве или в системе."
        if status.get("input_volume") == 0:
            return "Уровень входа микрофона установлен в ноль."
        return None

    def _take(self, pcm):
        if len(pcm) != BLOCK_BYTES:
            return
        speaking = self.speech(pcm)
        if not self.audio:
            if speaking:
                self.audio.extend(self.previous)
                self.audio.extend(pcm)
                self.previous = b""
            else:
                self.previous = bytes(pcm)
            return
        self.audio.extend(pcm)
        if (self.quiet_tail(pcm) >= QUIET_TAIL or not (speaking or self.activity(pcm))
                or len(self.audio) >= self.SEGMENT_SECONDS * BLOCK_BYTES):
            self._recognise()

    def _recognise(self):
        pcm, self.audio, self.previous = bytes(self.audio), bytearray(), b""
        if self.aborted.is_set():
            return
        self.dictation.partial("Распознаю…")
        try:
            text = self.service.listening.model.transcribe(Batch(0, 1, [], pcm).wav(), token=self.token).strip()
        except Exception:
            if self.token.is_set():
                return                           # cancelled by the owner's stop or a safeguard
            self.failures += 1
            if self.failures >= self.FAILURES_ALLOWED:
                self._fail("Распознавание речи недоступно: локальный Whisper не отвечает. Повторите позже.")
            else:
                self.dictation.partial("Слушаю…")
            return
        self.failures = 0
        if text and not self.token.is_set():
            self.dictation.hear(text, session=self)
        self.dictation.partial("Слушаю…")

    def _fail(self, reason):
        self.dictation.session_failed(self, reason)
