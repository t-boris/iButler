"""The owner panel's written conversation with the butler agent (ambient 0.8.0).

The owner types, or dictates and reviews, a message in the panel's Display page and reads the answer
there. It is the same agent, tools and memory as in Telegram, but a separate OpenClaw session
(``agent:butler:panel``): the gateway's chat endpoint hard-codes ``deliver: false`` (openclaw dist/openai-http),
so the reply of a panel turn is never delivered to a channel, and a Telegram turn is answered by OpenClaw
in Telegram without passing through here.

Boundaries:
- The thread is stored locally until the owner presses Clear. It survives service restarts,
  pauses and elapsed days, with no automatic count pruning. It is never given to
  feedback/episode capture, MCP or any other channel; ``ui-state`` is a local Unix-socket response.
- Nothing is spoken. The dialogue coordinator that voices replies is not used.
- A failure is shown as a failure with its reason. There is no silent fallback to the local model for
  typed text: that model cannot see the agent's tools and must not pose as it.
- Typed text carries no voice verification: the local panel of a logged-in Mac is the authority, the
  same as a keyboard at that Mac. Log lines carry the outcome and the length, never the text.
"""
from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import deque

from .brain import AgentBrain, BrainSilent, BrainUnavailable, strip_usage
from .session_follow import SessionFollower

LOG = logging.getLogger("butler.ambient")
SESSION = "agent:butler:panel"
MAX_TEXT = 4000          # characters of one message
MAX_CONTEXT = 1500       # characters of the hidden context a card button adds for the agent
MAX_ENTRIES = 80         # asked + answered rows kept
MAX_QUEUED = 8           # accepted turns waiting for the single worker
RETENTION = 86400.0
FOLLOW_FAST, FOLLOW_SLOW, FOLLOW_ACTIVE = 4.0, 30.0, 1800.0   # poll seconds while a follow-up is likely, otherwise; and how long "likely" lasts
FOLLOW_LABEL = "Butler · фоновая задача"
SILENT_TEXT = "Butler решил не отвечать на это сообщение."


def panel_preamble(now_text):
    """The channel instructions for a written turn: facts about the channel, never media or other people's words."""
    return (f"[Панель Butler на Mac Studio, {now_text}. Это письменный чат владельца с тобой: отвечай на языке владельца, "
            "по существу и без озвучивания; markdown, списки и ссылки уместны. Ответ остаётся в панели: не дублируй его "
            "в Telegram, пока владелец сам не попросит. Схему, архитектуру, шаги, числа и выбор между вариантами показывай карточкой "
            "на дисплее (инструмент butler_display; правила, шаблоны и кнопки: docs/ambient-and-display.md) и пиши кнопки вместо "
            "вопроса словами; если дисплей отказал, дай то же текстом прямо в ответе. Перед реальной долгой работой кратко назови, что начал, и дай честную "
            "оценку времени; не выдумывай прогресс. Перед действием с внешним эффектом спроси подтверждение одной фразой. "
            "Если сообщение явно адресовано не тебе, ответь ровно NO_REPLY.]")


def readable(error):
    """The reason a turn failed, in words the owner can act on. Built from the brain's short codes only."""
    code = str(error)
    if code.startswith("gateway http"):
        return "шлюз OpenClaw ответил ошибкой (" + code.split()[-1] + ")"
    if code == "gateway token unavailable":
        return "нет доступа к шлюзу OpenClaw: токен не найден"
    if code == "empty reply":
        return "агент вернул пустой ответ"
    if code.startswith("agent-error"):
        return "агент сообщил об ошибке; результат не подтверждён"
    if code == "agent response too long":
        return "ответ агента слишком длинный"
    if code == "cancelled":
        return "отменено"
    if code in {"ConnectionRefusedError", "ConnectionResetError", "BrokenPipeError", "RemoteDisconnected"}:
        return "шлюз OpenClaw не отвечает: проверьте, что он запущен"
    if code in {"TimeoutError", "timeout"}:
        return "агент не ответил вовремя"
    return "агент недоступен (" + code[:40] + ")"


def held_back(text):
    """A stream that so far could still become the silence sentinel is not shown yet."""
    probe = text.strip().strip("[]. ").upper().replace(" ", "_")
    return bool(probe) and len(probe) <= 8 and "NO_REPLY".startswith(probe)


class PanelChat:
    def __init__(self, service, *, brain=None, follower=None):
        self.service = service
        self.clock = service.clock
        config = service.config if isinstance(getattr(service, "config", None), dict) else {}
        # The panel runs on the agent's own model unless the owner configures an override: the voice path keeps its fast
        # override (brain_model_override); an empty string sends no model header, so the butler agent's configured model applies.
        own_brain = brain is None
        self.brain = brain or AgentBrain(dict(config, brain_session=str(config.get("panel_session", SESSION)),
                                              brain_model_override=str(config.get("panel_model_override", ""))),
                                         service.store, clock=self.clock)
        # Results that arrive after a turn ended (sub-agent announcements) are answered in the session, not in our request.
        # ``follower`` None = follow when the panel built its own brain; False = never (hermetic tests); or an instance.
        self.follower = (SessionFollower(self.brain) if own_brain else None) if follower is None else (follower or None)
        self.follow_thread = None
        self.last_activity = None
        self.condition = threading.Condition(threading.RLock())
        self.entries = []              # owner row, then its answer row, oldest first
        self.queue = deque()           # turn ids waiting for the worker
        self.epoch = 0
        self.cancel = threading.Event()
        self.ended = threading.Event()
        self.worker = None
        self.counts = {"sent": 0, "answered": 0, "silent": 0, "failed": 0, "followup": 0}
        from .local_history import LocalHistory
        self.journal = LocalHistory(service.store, "panel-chat")
        self.entries = self.journal.load()
        for entry in self.entries:
            if entry.get("state") in {"queued", "running", "streaming"}:
                entry.update(state="failed", error="Работа прервана перезапуском службы; автоматический повтор не выполнялся.")
                self.journal.put(entry)

    def _persist(self):
        for entry in self.entries:
            self.journal.put({k: v for k, v in entry.items() if k != "context"})


    # -- the owner's side -------------------------------------------------------------------

    @staticmethod
    def validate(text):
        if not isinstance(text, str):
            raise ValueError("сообщение должно быть текстом")
        text = text.strip()
        if not text:
            raise ValueError("пустое сообщение")
        if len(text) > MAX_TEXT:
            raise ValueError(f"сообщение длиннее {MAX_TEXT} символов")
        if "\x00" in text:
            raise ValueError("сообщение содержит недопустимые символы")
        return text

    def send(self, text, *, context=None):
        """Accept one message and return at once; the answer arrives in the next ``ui-state`` polls.

        ``context`` is what the agent needs to understand a card button (which card, which options). It goes to the agent
        in front of the text, is never shown in the thread, and is never logged."""
        text = self.validate(text)
        if context is not None and (not isinstance(context, str) or not context.strip() or len(context) > MAX_CONTEXT):
            raise ValueError("invalid context")
        from .recordings import navigate_voice, voice_target
        if voice_target(text, self.clock()) is not None:
            with self.service.lock:
                navigate_voice(self.service, text, authorized=True)
            return {"queued": True, "id": "local-recordings", "local": True}
        with self.condition:
            if self.ended.is_set():
                raise ValueError("панель закрывается")
            self._expire()
            if len(self.queue) >= MAX_QUEUED:
                raise ValueError("слишком много сообщений ждут ответа; дождитесь ответа Butler")
            turn = uuid.uuid4().hex[:12]
            now = self.clock()
            self.entries.append({"id": turn, "at": now, "who": "owner", "text": text, "state": "sent", "turn": turn,
                                 **({"context": context.strip()} if context else {})})
            self.entries.append({"id": turn + ".r", "at": now, "who": "butler", "text": "", "state": "queued", "turn": turn})
            self.queue.append(turn)
            self.counts["sent"] += 1
            self._trim()
            self._persist()
            if self.worker is None or not self.worker.is_alive():
                self.worker = threading.Thread(target=self._loop, name="butler-panel-chat", daemon=True)
                self.worker.start()
            if self.follower is not None and (self.follow_thread is None or not self.follow_thread.is_alive()):
                self.follow_thread = threading.Thread(target=self._follow_loop, name="butler-panel-follow", daemon=True)
                self.follow_thread.start()
            self.condition.notify_all()
        LOG.info("panel chat turn %s accepted (%d characters)", turn, len(text))
        return {"queued": True, "id": turn}

    def clear(self, reason="owner"):
        """Forget the thread: the owner asked, or consent ended. A turn in flight is cancelled and its answer dropped."""
        with self.condition:
            self.epoch += 1
            self.cancel.set()
            self.cancel = threading.Event()
            if reason == "owner":
                self.journal.clear()
                self.entries.clear()
            else:
                for entry in self.entries:
                    if entry.get("state") in {"queued", "running", "streaming"}:
                        entry.update(state="failed", error="Работа прервана остановкой службы; автоматический повтор не выполнялся.")
                self._persist()
            self.queue.clear()
            self.last_activity = None
            if self.follower is not None:
                self.follower.reset()
            self.condition.notify_all()
        return {"cleared": True, "reason": str(reason)[:40]}

    def stop(self):
        self.ended.set()
        self.clear("service-stopped")
        for thread in (self.worker, self.follow_thread):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=2)

    # -- what the panel sees ----------------------------------------------------------------

    def snapshot(self):
        with self.condition:
            self._expire()
            owner = getattr(self.service, "owner_name", None)
            name = owner() if callable(owner) else "Владелец"
            rows = []
            for entry in self.entries:
                rows.append({"id": entry["id"], "at": entry["at"], "turn": entry["turn"], "state": entry["state"],
                             "who": name if entry["who"] == "owner" else (FOLLOW_LABEL if entry.get("kind") == "followup" else "Butler"),
                             "from": entry["who"], "kind": entry.get("kind", "turn"),
                             "text": entry["text"], **({"error": entry["error"]} if entry.get("error") else {})})
            return {"entries": rows, "busy": any(e["who"] == "butler" and e["state"] in {"queued", "running", "streaming"}
                                                 for e in self.entries),
                    "session": getattr(self.brain, "session_key", SESSION), "counts": dict(self.counts),
                    **({"follow": self.follower.snapshot()} if self.follower is not None else {})}

    # -- the worker -------------------------------------------------------------------------

    def _loop(self):
        while not self.ended.is_set():
            with self.condition:
                turn = None
                while self.queue and turn is None:
                    candidate = self.queue.popleft()
                    if self._row(candidate + ".r") is not None:
                        turn = candidate
                if turn is None:
                    self.condition.wait(.2)
                    continue
                epoch, cancel = self.epoch, self.cancel
                self._row(turn + ".r")["state"] = "running"
                asked = self._row(turn)
                text, context = asked["text"], asked.get("context")
            self._run(turn, text, epoch, cancel, context)

    def _row(self, row_id):
        return next((e for e in self.entries if e["id"] == row_id), None)

    def _set(self, turn, epoch, **fields):
        """Update the answer row unless the thread was cleared meanwhile. Returns whether it still exists."""
        with self.condition:
            row = self._row(turn + ".r")
            if epoch != self.epoch or row is None:
                return False
            row.update(fields)
            self.journal.put({k: v for k, v in row.items() if k != "context"})
            self.condition.notify_all()
            return True

    def _run(self, turn, text, epoch, cancel, context=None):
        streamed = []

        def on_text(delta):
            streamed.append(delta)
            shown = strip_usage("".join(streamed)).lstrip()
            if shown and not held_back(shown):
                self._set(turn, epoch, state="streaming", text=shown)

        self._drain()          # a result that arrived before this turn is shown first, not swallowed by the cursor
        preamble = panel_preamble(time.strftime("%Y-%m-%d %H:%M", time.localtime(self.clock())))
        try:
            reply = self.brain.ask(f"{context}\n\n{text}" if context else text, preamble, token=cancel, on_text=on_text, speech=False)
            result = {"state": "answered", "text": reply}
        except BrainSilent:
            result = {"state": "silent", "text": SILENT_TEXT}
        except BrainUnavailable as error:
            result = {"state": "failed", "text": "", "error": readable(error)}
        except Exception as error:  # the class only: a message may quote the owner's text
            LOG.warning("panel chat turn %s failed: %s", turn, type(error).__name__)
            result = {"state": "failed", "text": "", "error": "внутренняя ошибка панели (" + type(error).__name__ + ")"}
        if self._set(turn, epoch, **result):      # false when the thread was cleared meanwhile: the answer is dropped
            with self.condition:
                self.counts[result["state"]] += 1
            LOG.info("panel chat turn %s %s", turn, result["state"])
        self._after_turn(result["state"])

    def _after_turn(self, state):
        """Whatever the gateway recorded during an answered turn is accounted for: the panel showed it as the reply.

        A FAILED turn leaves the cursor where it was: the stream broke, but the agent may still finish and record
        the answer, and that answer must reach the thread as a follow-up instead of being marked as seen."""
        with self.condition:
            self.last_activity = self.clock()
        if self.follower is not None and state != "failed":
            try:
                self.follower.mark()
            except Exception as error:
                LOG.warning("panel chat follow cursor failed: %s", type(error).__name__)

    # -- results that arrive after the turn ended ----------------------------------------------

    def _drain(self):
        """Append the assistant messages the session produced on its own (sub-agent results, long work)."""
        follower = self.follower
        if follower is None:
            return
        try:
            with self.condition:
                epoch = self.epoch
                shown = [e["text"] for e in self.entries if e["who"] == "butler" and e["state"] == "answered"][-6:]
            texts = follower.poll(shown)
        except Exception as error:      # never break a turn or the loop; the class only, the text may be the owner's
            LOG.warning("panel chat follow-up read failed: %s", type(error).__name__)
            return
        if not texts:
            return
        with self.condition:
            if epoch != self.epoch or self.ended.is_set():
                return                   # the thread was cleared meanwhile: the late message is dropped with it
            now = self.clock()
            for text in texts:
                fid = uuid.uuid4().hex[:12]
                self.entries.append({"id": fid, "at": now, "who": "butler", "text": text, "state": "answered",
                                     "turn": fid, "kind": "followup"})
                self.counts["followup"] += 1
            self.last_activity = now
            self._trim()
            self._persist()
            self.condition.notify_all()
        LOG.info("panel chat follow-up received (%d)", len(texts))

    def _follow_loop(self):
        while not self.ended.is_set():
            with self.condition:
                quiet = not self.entries or bool(self._active())     # nothing to follow, or our own turn is streaming
                likely = self.last_activity is not None and self.clock() - self.last_activity < FOLLOW_ACTIVE
            if not quiet:
                self._drain()
            self.ended.wait(FOLLOW_FAST if likely else FOLLOW_SLOW)

    def _active(self):
        """Turns whose answer is still waiting, running or streaming: these are never dropped."""
        return {e["turn"] for e in self.entries if e["who"] == "butler" and e["state"] in {"queued", "running", "streaming"}}

    def _expire(self):
        # Owner 2026-10-28: history remains until Clear, regardless of age.
        pass

    def _trim(self):
        # No automatic row-count deletion. Queue and per-message limits remain.
        pass
