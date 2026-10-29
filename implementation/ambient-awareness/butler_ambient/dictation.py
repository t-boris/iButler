"""Dictation into the panel's message field (ambient 0.8.0).

The panel app has no microphone permission (it belongs to «Butler Awareness»), so «Диктовать» arms
the service's own hearing path for this one purpose. While a dictation listens, every utterance that
finishes is recognised by the same resident Whisper, with its phantom and confidence filters, and
returned to the panel as text for the owner to read, correct and send. Nothing else receives those
words: no feedback episode or transcript revision, no voice dialogue, no log line, no event text.

The recognised text lives here, in memory, until the next dictation or two minutes after it ended.
Dictation never opens, extends or confirms an owner session: it only turns speech into text for the
local panel. Whoever speaks while it listens is transcribed; the owner reads the result before
anything is sent.
"""
from __future__ import annotations

import threading
import uuid

from .panel_chat import MAX_TEXT

SILENCE_AFTER = 2.0        # seconds after the last recognised words, once nobody is speaking any more
NOTHING_HEARD_AFTER = 30.0
STOP_GRACE = 8.0          # how long «Остановить» waits for an utterance that is already being recognised
TOTAL_LIMIT = 120.0
KEEP_RESULT = 120.0
SPEAKING = {"capturing-speech", "listening", "transcription", "awaiting-continuation"}
NOTHING = "Ничего не распознано. Проверьте уровень микрофона и повторите."


class Dictation:
    def __init__(self, service):
        self.service = service
        self.lock = threading.RLock()
        self.serial = 0
        self._reset()

    def _reset(self):
        self.state, self.id, self.reason, self.note = "idle", None, None, None
        self.text = self.partial_text = ""
        self.armed_at = self.last_text_at = self.finished_at = self.stopping_at = None

    # -- the panel's commands ---------------------------------------------------------------

    def start(self):
        with self.service.lock, self.lock:
            self._advance()
            if self.state == "listening":
                return self._view()
            self._reset()
            self.serial += 1
            self.id = f"dictation-{self.serial}-{uuid.uuid4().hex[:6]}"
            reason = self.unavailable()
            if reason:
                self.state, self.reason, self.finished_at = "failed", reason, self.service.clock()
                return self._view()
            self.state, self.armed_at = "listening", self.service.clock()
            return self._view()

    def stop(self):
        """The owner is done. A phrase already being recognised is still delivered; nothing new is begun after it."""
        with self.service.lock, self.lock:
            self._advance()
            if self.state == "listening":
                if self._speaking():
                    self.stopping_at = self.service.clock()
                else:
                    self._finish()
            return self._view()

    # -- the hearing path -------------------------------------------------------------------

    def listening(self):
        with self.lock:
            self._advance()
            return self.state == "listening"

    def hear(self, text):
        """One finished utterance, recognised. Called by the hearing path while this dictation listens."""
        with self.lock:
            if self.state != "listening":
                return False
            text = text.strip()
            if text:
                self.text = (self.text + " " + text).strip()[:MAX_TEXT]       # separate utterances: no boundary merging
                self.last_text_at = self.service.clock()
            self.partial_text = ""
            if len(self.text) >= MAX_TEXT:
                self._finish()
            return True

    def partial(self, text):
        """The latest draft of the utterance being spoken, for the panel's status line only."""
        with self.lock:
            if self.state == "listening":
                self.partial_text = text.strip()[-300:]

    def interrupt(self, reason="hearing-stopped"):
        """The microphone stopped listening (pause, lost permission, setup). What was heard is kept."""
        with self.lock:
            if self.state != "listening":
                return
            if self.text:
                self.note = "Микрофон перестал слушать; продиктованное сохранено."
                self._finish()
            else:
                self.state, self.finished_at = "failed", self.service.clock()
                self.reason = "Микрофон Butler перестал слушать: пауза или смена доступа."
                self.partial_text = ""

    # -- state ------------------------------------------------------------------------------

    def unavailable(self):
        """Why dictation cannot start now, in words for the owner; None when the hearing path can take it."""
        service = self.service
        listening = getattr(service, "listening", None)
        if listening is None:
            return "Слух Butler не настроен: нет локального распознавания речи."
        if not service.gate.enabled:
            return "Наблюдение на паузе: нажмите «Продолжить», чтобы включить микрофон."
        if service.stop_event.is_set():
            return "Сервис останавливается."
        if getattr(getattr(service, "owner_setup", None), "capturing", False):
            return "Идёт запись эталона голоса: диктовка недоступна."
        if not listening.enabled:
            return ("Микрофон Butler сейчас не слушает: включите «Постоянный анализ» "
                    "или дождитесь подтверждения владельца перед камерой.")
        voice = getattr(service, "voice", None)
        # An agent request can be active for minutes without producing sound.
        # Block only a reply that is actually being prepared for speech, not
        # the independent background request tracked by reply_in_progress.
        state = getattr(voice, "state", None)
        phase = getattr(voice, "phase", None)
        if state in {"speaking", "synthesizing"} or (state == "thinking" and phase == "reply"):
            return "Butler сейчас говорит: дождитесь окончания, иначе его голос попадёт в текст."
        inputs = getattr(service, "inputs", None)
        status = getattr(inputs, "audio_status", None)
        status = status if isinstance(status, dict) else {}
        signal = getattr(inputs, "input_signal", None)
        fresh = isinstance(signal, dict) and 0 <= service.clock() - signal.get("at", float("-inf")) <= 2.5
        if status.get("input_muted") is True:
            return "Микрофон выключен на устройстве или в системе."
        if status.get("input_volume") == 0:
            return "Уровень входа микрофона установлен в ноль."
        if not fresh:
            return "С микрофона нет свежего звука: проверьте, что он подключён и не занят другим приложением."
        return None

    def _speaking(self):
        listening = getattr(self.service, "listening", None)
        return listening is not None and (listening.phase in SPEAKING or listening.processing or not listening.jobs.empty()
                                          or bool(listening.audio))

    def _advance(self):
        now = self.service.clock()
        if self.state == "listening":
            listening = getattr(self.service, "listening", None)
            if listening is None or not listening.enabled:
                self.interrupt()
            elif now - self.armed_at > TOTAL_LIMIT:
                self._finish()
            elif self.stopping_at is not None:
                if not self._speaking() or now - self.stopping_at >= STOP_GRACE:
                    self._finish()
            elif self.text and now - self.last_text_at >= SILENCE_AFTER and not self._speaking():
                self._finish()
            elif not self.text and now - self.armed_at >= NOTHING_HEARD_AFTER and not self._speaking():
                self._finish()
        elif self.state in {"done", "failed"} and self.finished_at is not None and now - self.finished_at > KEEP_RESULT:
            self._reset()

    def _finish(self):
        self.state, self.finished_at, self.partial_text = "done", self.service.clock(), ""
        if not self.text and not self.note:
            self.note = NOTHING

    def _view(self):
        listening = self.state == "listening"
        return {"state": self.state, "id": self.id, "text": self.text, "partial": self.partial_text if listening else "",
                "stopping": listening and self.stopping_at is not None, "reason": self.reason, "note": self.note,
                "seconds": round(self.service.clock() - self.armed_at, 1) if listening and self.armed_at is not None else None}

    def snapshot(self):
        with self.lock:
            self._advance()
            return self._view()
