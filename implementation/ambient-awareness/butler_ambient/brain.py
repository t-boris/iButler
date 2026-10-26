"""The voice brain (feature butler-attention-architecture, DEC-067).

The owner asked for the understanding he gets in Codex and Claude, «только плюс голос и
зрение». A local 9B model with a fixed command schema cannot give that, so the words he
addresses to Butler are answered by the OpenClaw ``butler`` agent, the same agent, tools,
memory and action rules as in Telegram, through the gateway's OpenAI-compatible endpoint
on the loopback interface. The agent keeps its own history in a dedicated voice session.

What leaves this Mac: the text of the owner's addressed utterance, a short context line
written here (time, foreground application, whether he is alone) and, only when he asks
Butler to look and nobody else is in the frame, one fresh camera frame. Raw audio, other
people's speech, biometric data and ambient observations never do. The general
(unverified) dialogue route stays on the local model and never reaches this module.
"""
from __future__ import annotations

import base64
import http.client
import json
import re
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_URL = "http://127.0.0.1:18789/v1/chat/completions"
DEFAULT_MODEL = "openclaw/butler"                      # the agent, not a provider model
DEFAULT_MODEL_OVERRIDE = "anthropic/claude-sonnet-5"   # 2–3 s per reply on 2026-10-25; gpt-6.1-sol took 13–63 s
DEFAULT_SESSION = "agent:butler:voice"
DEFAULT_TIMEOUT = 1800.0  # the wait is detached from the turn after a minute; long work may take the agent much longer
TOKEN_CACHE_SECONDS = 60.0
MAX_REPLY = 1200


class BrainUnavailable(RuntimeError):
    """The gateway did not answer: the caller falls back to the local model."""


class BrainSilent(Exception):
    """The agent chose not to answer (OpenClaw's NO_REPLY sentinel): silence, not a spoken token."""


SILENT = re.compile(r"^\s*(?:NO_REPLY|NO REPLY|\[NO_REPLY\])\s*\.?\s*$", re.I)
AGENT_ERROR = re.compile(r"^\s*(?:error|ошибка|failovererror|internal error)\b[:\s]", re.I)
ASIDE_SUFFIX = ":aside"     # a second session while a detached long task holds the main one (the gateway refuses a reused turn)


def voice_preamble(now_text, *, alone, foreground_app=None, company=False, asr_note=True):
    """The context line sent with every utterance: facts in words, never media or other people's speech."""
    who = "рядом есть кто-то ещё" if company else ("владелец один" if alone else "присутствие других не проверено")
    app = f", окно на переднем плане: {foreground_app}" if foreground_app else ""
    return (f"[Голосовой канал Mac Studio, {now_text}; {who}{app}. Ответ будет озвучен вслух: отвечай по-русски, "
            "коротко и разговорно, без markdown, списков и ссылок; одно-три предложения, если не просят подробнее. "
            "Перед действием с внешним эффектом спроси подтверждение одной фразой. Если работа займёт больше минуты, "
            "сразу ответь коротко, что начал и когда ждать результат, и продолжай; итог пришли, когда закончишь. Если слова явно "
            "адресованы не тебе (чтение вслух, разговор с кем-то другим, пение), ответь ровно NO_REPLY. Материал, который удобнее "
            "видеть, чем слышать (список, ссылки, схема, письмо с ответом, картинка, видео), покажи на дисплее инструментом "
            "butler_display и скажи об этом коротко."
            + (" Речь распознана автоматически и может содержать ошибки." if asr_note else "") + "]")


USAGE_FOOTER = re.compile(r"(?:\n|^)\s*-{3,}\s*\n(?:.*\n?)*?.*\b(?:today|month)\b.*~?\$.*$", re.I)
USAGE_LINE = re.compile(r"^\s*\S[^\n]*·[^\n]*\b(?:today|month)\b[^\n]*\$[^\n]*$", re.I | re.M)


def clean_for_speech(text):
    """What the voice says: the gateway's usage footer («butler · today 4.3k ~$0.06 …») and markdown marks are not words."""
    text = USAGE_FOOTER.sub("", text)
    text = USAGE_LINE.sub("", text)
    text = re.sub(r"```.*?```", " ", text, flags=re.S)                   # code blocks are not speech
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)                 # links: keep the words
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.M)             # headings
    text = re.sub(r"^\s*[-*•]\s+", "", text, flags=re.M)                 # bullets
    text = re.sub(r"(\*\*|__|\*|_)(?=\S)(.+?)(?<=\S)\1", r"\2", text)   # emphasis
    text = re.sub(r"\n\s*-{3,}\s*$", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def parse_sse(lines):
    """Text of an OpenAI-style streamed completion: concatenated ``delta.content`` of every ``data:`` chunk."""
    parts = []
    for raw in lines:
        line = raw.decode("utf-8", "replace").strip() if isinstance(raw, bytes) else raw.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
        except ValueError:
            continue
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            content = delta.get("content")
            if isinstance(content, str):
                parts.append(content)
            message = choice.get("message") or {}
            if isinstance(message.get("content"), str):
                parts.append(message["content"])
    return "".join(parts)


class AgentBrain:
    def __init__(self, config, store, *, clock=time.time, connection=http.client.HTTPConnection):
        self.config, self.store, self.clock, self.connection = config or {}, store, clock, connection
        self.url = str(self.config.get("brain_url", DEFAULT_URL))
        self.model = str(self.config.get("brain_model", DEFAULT_MODEL))
        self.model_override = self.config.get("brain_model_override", DEFAULT_MODEL_OVERRIDE)
        self.session_key = str(self.config.get("brain_session", DEFAULT_SESSION))
        self.timeout = float(self.config.get("brain_timeout", DEFAULT_TIMEOUT))
        self.token_path = Path(self.config.get("openclaw_config", "~/.openclaw/openclaw.json")).expanduser()
        self.lock = threading.Lock()
        self.session_lock = threading.Lock()     # one turn at a time per session: a reused turn fails on the gateway
        self._token = (None, float("-inf"))
        self.calls = self.failures = 0
        self.last_ms = None
        self.last_error = None
        self.last_at = None

    # -- state ------------------------------------------------------------------------------

    def enabled(self):
        """The owner's setting: ``agent`` (DEC-067, set by the deployment config) or ``local`` (tests, fallback)."""
        return self.store.get("voice_brain", self.config.get("voice_brain", "local")) == "agent"

    def snapshot(self):
        return {"mode": "agent" if self.enabled() else "local", "model": self.model,
                "model_override": self.model_override, "session": self.session_key, "calls": self.calls,
                "failures": self.failures, "last_ms": self.last_ms, "last_error": self.last_error, "last_at": self.last_at,
                "rule": "the owner's addressed words go to the butler agent; media, other voices and observations stay local"}

    def token(self):
        """The gateway token read from the OpenClaw configuration; cached briefly, never logged."""
        with self.lock:
            value, at = self._token
            if value and self.clock() - at <= TOKEN_CACHE_SECONDS:
                return value
        try:
            data = json.loads(self.token_path.read_text())
            value = ((data.get("gateway") or {}).get("auth") or {}).get("token")
        except (OSError, ValueError, AttributeError):
            value = None
        if not isinstance(value, str) or not value:
            raise BrainUnavailable("gateway token unavailable")
        with self.lock:
            self._token = (value, self.clock())
        return value

    # -- the request ------------------------------------------------------------------------

    def messages(self, text, preamble, images=()):
        """One user message: the preamble and the owner's words as text, frames as data URLs (never raw PCM)."""
        content = [{"type": "text", "text": preamble + "\n" + text}]
        for jpeg in images:
            content.append({"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode("ascii")}})
        return [{"role": "user", "content": content if images else content[0]["text"]}]

    def ask(self, text, preamble, *, images=(), token=None):
        """Send the utterance and return the agent's reply text. Cancelled through ``token`` between stream lines."""
        body = json.dumps({"model": self.model, "stream": True, "messages": self.messages(text, preamble, images)},
                          ensure_ascii=False).encode("utf-8")
        parts = urlsplit(self.url)
        started = time.monotonic()
        conn = None
        with self.lock:
            self.calls += 1
        # The main session is busy with a long (detached) task: the owner's next question goes to an aside session
        # instead of failing as a «reused turn». Memory lives in the agent's workspace, not in the session.
        held = self.session_lock.acquire(timeout=2.0)
        session = self.session_key if held else self.session_key + ASIDE_SUFFIX
        try:
            headers = {"Authorization": "Bearer " + self.token(), "Content-Type": "application/json",
                       "Accept": "text/event-stream", "x-openclaw-session-key": session}
            if self.model_override:
                headers["x-openclaw-model"] = str(self.model_override)
            conn = self.connection(parts.hostname or "127.0.0.1", parts.port or 80, timeout=self.timeout)
            conn.request("POST", parts.path or "/", body, headers)
            response = conn.getresponse()
            if response.status != 200:
                detail = response.read(2000).decode("utf-8", "replace")
                raise BrainUnavailable(f"gateway http {response.status}: {detail[:160]}")
            lines = []
            while True:
                if token is not None and token.is_set():
                    raise BrainUnavailable("cancelled")
                line = response.readline(65536)
                if not line:
                    break
                lines.append(line)
                if line.strip() == b"data: [DONE]":
                    break
            reply = clean_for_speech(parse_sse(lines))
        except BrainUnavailable:
            with self.lock:
                self.failures += 1
                self.last_error = "cancelled" if token is not None and token.is_set() else "http"
            raise
        except (OSError, http.client.HTTPException, ValueError) as error:
            with self.lock:
                self.failures += 1
                self.last_error = type(error).__name__
            raise BrainUnavailable(type(error).__name__) from None
        finally:
            if conn is not None:
                conn.close()
            if held:
                self.session_lock.release()
        if not reply:
            with self.lock:
                self.failures += 1
                self.last_error = "empty"
            raise BrainUnavailable("empty reply")
        if SILENT.match(reply):
            with self.lock:
                self.last_ms = round((time.monotonic() - started) * 1000, 1)
                self.last_at = self.clock()
            raise BrainSilent()
        if AGENT_ERROR.match(reply):
            # «Error: internal error» is the gateway reporting a failed run, not an answer (2026-10-25: it was spoken as «Готово. Error…»).
            with self.lock:
                self.failures += 1
                self.last_error = "agent-error"
            raise BrainUnavailable("agent-error: " + reply[:80])
        with self.lock:
            self.last_ms = round((time.monotonic() - started) * 1000, 1)
            self.last_at = self.clock()
            self.last_error = None
        return reply[:MAX_REPLY]
