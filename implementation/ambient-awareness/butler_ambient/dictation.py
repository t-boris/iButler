"""Dictation into the panel's message field (ambient 0.8.0; own microphone session since 0.12.4, DEC-049).

The panel app has no microphone permission (it belongs to «Butler Awareness»), so «Диктовать» arms a hearing path of the
service for this one purpose. Which one depends on observation at the moment of the press:

* observation on: the service's own hearing path. While a dictation listens, every utterance that finishes is recognised
  by the same resident Whisper, with its phantom and confidence filters, and returned to the panel as text.
* observation paused: its own short microphone session (``dictation_capture``): only the microphone, no camera, no
  screen, no identity checks, closed again by «Остановить» or by the safeguards below. Pause keeps stopping every
  background source; the owner's button is an explicit, bounded action.

Nothing else receives those words in either mode: no feedback episode or transcript revision, no voice dialogue, no log
line, no event text. The recognised text lives here, in memory, until the next dictation or two minutes after it ended.
Dictation never opens, extends or confirms an owner session: it only turns speech into text for the local panel. Whoever
speaks while it listens is transcribed; the owner reads the result before anything is sent.
"""
from __future__ import annotations

import threading
import uuid

from .dictation_capture import open_capture
from .panel_chat import MAX_TEXT

SILENCE_AFTER = 2.0        # seconds after the last recognised words, once nobody is speaking any more
NOTHING_HEARD_AFTER = 30.0
STOP_GRACE = 90.0         # bound a final in-flight local recognition after the stop boundary
TOTAL_LIMIT = 120.0
KEEP_RESULT = 120.0
# Safeguards of the own microphone session: it must never stay open by accident (DEC-049).
OWN_IDLE_LIMIT = 120.0     # seconds without recognised words (counted from the press or the last words)
OWN_TOTAL_LIMIT = 1200.0   # twenty minutes in total
PANEL_GONE_AFTER = 15.0    # the panel polls about once a second; silence for this long means it is gone
SPEAKING = {"capturing-speech", "listening", "transcription", "awaiting-continuation"}
NOTHING = "Ничего не распознано. Проверьте уровень микрофона и повторите."
NO_RECOGNISER = "Слух Butler не настроен: нет локального распознавания речи."


class Dictation:
    def __init__(self, service):
        self.service = service
        self.lock = threading.RLock()
        self.serial = 0
        self.capture_factory = open_capture       # replaceable in tests; builds the own microphone session
        self._reset()

    def _reset(self):
        self.state, self.id, self.reason, self.note = "idle", None, None, None
        self.text = self.partial_text = ""
        self.armed_at = self.last_text_at = self.finished_at = self.stopping_at = None
        self.stop_boundary_pending = False
        self.capture, self.own, self.last_polled = None, False, None

    # -- the panel's commands ---------------------------------------------------------------

    def start(self):
        with self.service.lock, self.lock:
            self._advance()
            if self.state == "listening":
                self.last_polled = self.service.clock()
                return self._view()
            self._reset()
            self.serial += 1
            self.id = f"dictation-{self.serial}-{uuid.uuid4().hex[:6]}"
            reason = self.unavailable()
            if reason:
                self.state, self.reason, self.finished_at = "failed", reason, self.service.clock()
                return self._view()
            now = self.service.clock()
            if not self.service.gate.enabled:
                reason = self._open_own_session()
                if reason:
                    self.state, self.reason, self.finished_at = "failed", reason, now
                    return self._view()
                self.state, self.armed_at, self.last_polled = "listening", now, now
                self.partial_text = "Включаю микрофон…"
                return self._view()
            self.state, self.armed_at, self.last_polled = "listening", now, now
            boundary = getattr(self.service.listening, 'begin_dictation', None)
            if callable(boundary): boundary(self.id)
            return self._view()

    def _open_own_session(self):
        """Open the dedicated microphone session; the reason in the owner's words when that is impossible."""
        capture = None
        try:
            capture = self.capture_factory(self.service, self)
            self.capture, self.own = capture, True      # before start(): the worker asks about its session at once
            capture.start()
        except Exception:                               # the cause (device, permission, helper) is not for the panel
            self.capture, self.own = None, False
            if capture is not None:
                try: capture.abort()
                except Exception: pass
            return "Не удалось открыть микрофон: проверьте, что он подключён и не занят другим приложением."
        return None

    def stop(self):
        """The owner is done. A phrase already being recognised is still delivered; nothing new is begun after it."""
        with self.service.lock, self.lock:
            self._advance()
            if self.state == "listening":
                self.last_polled = self.service.clock()
                if self.stopping_at is None:
                    if self.own:
                        self._begin_stop()
                    else:
                        self.stopping_at = self.service.clock()
                        boundary = getattr(self.service.listening, 'stop_dictation', None)
                        if callable(boundary):
                            self.stop_boundary_pending = True
                            boundary(self.id, self.stopping_at)
                        elif not self._speaking(): self._finish()
            return self._view()

    # -- the hearing path / the own session -------------------------------------------------

    def listening(self):
        """True while the service's hearing path should claim utterances for this dictation. The own session does its
        own hearing, so the hearing path must never claim anything for it."""
        with self.lock:
            self._advance()
            return self.state == "listening" and not self.own

    def hear(self, text, session=None):
        """One finished utterance, recognised. Called by the hearing path (no session) or by the own session while this
        dictation listens."""
        with self.lock:
            if self.state != "listening":
                return False
            if self.own:
                if session is None or session is not self.capture:
                    return False
            elif session is not None:
                return False
            text = text.strip()
            if text:
                self.text = (self.text + " " + text).strip()[:MAX_TEXT]       # separate utterances: no boundary merging
                self.last_text_at = self.service.clock()
            self.partial_text = ""
            if len(self.text) >= MAX_TEXT:
                if self.own:
                    self.note = "Достигнут максимальный размер текста."
                    if self.stopping_at is None:
                        self._begin_stop()
                else:
                    self.note = "Достигнут максимальный размер текста; нажмите «Остановить»."
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
            if self.own and self.capture is not None:
                self.capture.abort()
            if self.text:
                self.note = "Микрофон перестал слушать; продиктованное сохранено."
                self._finish()
            else:
                self.state, self.finished_at = "failed", self.service.clock()
                self.reason = ("Микрофон закрыт: диктовка прервана." if self.own
                               else "Микрофон Butler перестал слушать: пауза или смена доступа.")
                self.partial_text = ""

    # -- the own session's worker -------------------------------------------------------------

    def session_tick(self, session):
        """The worker's heartbeat, several times a second even when no panel polls: enforce the safeguards. False when
        this session no longer belongs to a listening dictation, so the worker must leave."""
        with self.lock:
            self._advance()
            return session is self.capture and self.own and self.state == "listening"

    def session_finished(self, session):
        """The microphone is closed and the last phrase is delivered (or there was none)."""
        with self.lock:
            if session is self.capture and self.state == "listening":
                self._finish()

    def session_failed(self, session, reason):
        """The session cannot go on (muted microphone, no sound, recogniser down). The words so far are kept."""
        with self.lock:
            if session is not self.capture or self.state != "listening":
                return
            self._end_own(reason, reason)

    # -- state ------------------------------------------------------------------------------

    def unavailable(self):
        """Why dictation cannot start now, in words for the owner; None when a hearing path can take it."""
        service = self.service
        listening = getattr(service, "listening", None)
        if listening is None:
            return NO_RECOGNISER
        own = not service.gate.enabled
        if own and getattr(listening, "model", None) is None:
            return NO_RECOGNISER
        if service.stop_event.is_set():
            return "Сервис останавливается."
        if getattr(getattr(service, "owner_setup", None), "capturing", False):
            return "Идёт запись эталона голоса: диктовка недоступна."
        if not own and not listening.enabled:
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
        if own:
            return None                      # the own session checks the microphone itself, once it is open
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
            if self.own:
                self._advance_own(now)
                return
            listening = getattr(self.service, "listening", None)
            if listening is None or not listening.enabled:
                self.interrupt()
            elif self.stopping_at is not None:
                if (not self.stop_boundary_pending and not self._speaking()) or now - self.stopping_at >= STOP_GRACE:
                    self._finish()
        elif self.state in {"done", "failed"} and self.finished_at is not None and now - self.finished_at > KEEP_RESULT:
            self._reset()

    def _advance_own(self, now):
        """The safeguards: the own microphone session never stays open by accident."""
        service = self.service
        if service.stop_event.is_set():
            return self._end_own("Сервис останавливается; продиктованное сохранено.", "Сервис останавливается.")
        if getattr(getattr(service, "owner_setup", None), "capturing", False):
            return self._end_own("Идёт запись эталона голоса; продиктованное сохранено.",
                                 "Идёт запись эталона голоса: диктовка остановлена.")
        if service.gate.enabled:
            return self._end_own("Наблюдение включено: диктовка остановлена; продиктованное сохранено.",
                                 "Наблюдение включено: диктовка остановлена.")
        if self.last_polled is not None and now - self.last_polled > PANEL_GONE_AFTER:
            return self._end_own("Панель Butler перестала отвечать: микрофон закрыт; продиктованное сохранено.",
                                 "Панель Butler не отвечает: микрофон закрыт.")
        if self.stopping_at is not None:
            if now - self.stopping_at >= STOP_GRACE:
                self.capture.abort()
                self._finish()
            return
        if now - (self.last_text_at if self.last_text_at is not None else self.armed_at) >= OWN_IDLE_LIMIT:
            self._begin_stop("Микрофон закрыт: две минуты тишины.")
        elif now - self.armed_at >= OWN_TOTAL_LIMIT:
            self._begin_stop("Микрофон закрыт: предел времени диктовки, 20 минут.")

    def _begin_stop(self, note=None):
        """Ask the own session to close its microphone and deliver the last words; the worker then ends the dictation."""
        self.stopping_at = self.service.clock()
        if note and not self.note:
            self.note = note
        self.capture.request_stop()

    def _end_own(self, note, reason):
        """Close the own microphone now. What was heard is kept; with nothing heard the dictation fails with the reason."""
        if self.capture is not None:
            self.capture.abort()
        self.partial_text = ""
        if self.text:
            self.note = note
            self._finish()
        else:
            self.state, self.finished_at, self.reason = "failed", self.service.clock(), reason

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
            if self.state == "listening":
                self.last_polled = self.service.clock()       # the panel is alive: it polls about once a second
            self._advance()
            return self._view()
