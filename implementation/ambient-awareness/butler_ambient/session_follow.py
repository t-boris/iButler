"""Messages an OpenClaw session produces outside the panel's own requests (ambient 0.9.0).

The panel reads ONE streamed reply per typed message. An agent that hands work to a sub-agent ends its turn;
the result comes back minutes later as a new turn of the same gateway session and is answered there, in a
request nobody holds open. OpenClaw serves that session's transcript at ``GET /sessions/<key>/history``
(shared-secret auth, operator scope ``chat.history``, the same token as the chat endpoint), so the panel can
follow the session instead of only its own requests.

What this module guards:
- Read only: one bounded GET of the newest messages; nothing is written to the gateway.
- Only the assistant's own text is returned: tool calls, tool results and user rows (including the gateway's
  injected announcements) are never shown.
- A message is returned once (sequence cursor) and never when it is part of an answer the panel already shows
  (the transcript may be persisted a moment after the stream ends).
- Texts are neither logged nor kept here; failures are counted and classified only.
"""
from __future__ import annotations

import json
import threading
from urllib.parse import quote, urlsplit

from .brain import SILENT, BrainUnavailable, clean_for_panel

LIMIT = 40                  # newest messages per read: a poll every few seconds never falls behind this
MAX_BODY = 4_000_000        # bytes of one history response


def _normal(text):
    """Whitespace-insensitive form used only to recognise text the panel already shows."""
    return " ".join(str(text).split())


def _sequence(message):
    meta = message.get("__openclaw") if isinstance(message, dict) else None
    value = meta.get("seq") if isinstance(meta, dict) else None
    return value if type(value) is int else None


def _text(message):
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content
                       if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str))
    return ""


class SessionFollower:
    def __init__(self, brain, *, limit=LIMIT):
        self.brain, self.limit = brain, limit
        self.seen = None                      # highest sequence already accounted for; None until the first read
        self.lock = threading.Lock()
        self.counts = {"polls": 0, "followups": 0, "failures": 0, "gaps": 0}
        self.last_error = None                # a class or short code, never message text

    # -- the gateway ------------------------------------------------------------------------

    def _history(self):
        parts = urlsplit(self.brain.url)
        path = f"/sessions/{quote(self.brain.session_key, safe='')}/history?limit={self.limit}"
        conn = self.brain.connection(parts.hostname or "127.0.0.1", parts.port or 80, timeout=15)
        try:
            conn.request("GET", path, headers={"Authorization": "Bearer " + self.brain.token(), "Accept": "application/json"})
            response = conn.getresponse()
            if response.status == 404:        # the session does not exist yet: nothing to follow
                response.read(2000)
                return []
            if response.status != 200:
                response.read(2000)
                raise BrainUnavailable(f"history http {response.status}")
            body = response.read(MAX_BODY + 1)
        finally:
            try:
                conn.close()
            except OSError:
                pass
        if len(body) > MAX_BODY:
            raise BrainUnavailable("history too long")
        data = json.loads(body)
        items = data.get("items") if isinstance(data, dict) else None
        return [m for m in items if isinstance(m, dict)] if isinstance(items, list) else []

    def _read(self):
        """The newest messages, oldest first; ``None`` when the gateway could not be read (counted, never raised)."""
        with self.lock:
            self.counts["polls"] += 1
        try:
            messages = self._history()
        except BrainUnavailable as error:
            code = str(error)
        except (OSError, ValueError) as error:       # connection class or malformed JSON; never the message
            code = type(error).__name__
        else:
            self.last_error = None
            return sorted((m for m in messages if _sequence(m) is not None), key=_sequence)
        with self.lock:
            self.counts["failures"] += 1
            self.last_error = code[:40]
        return None

    # -- the cursor -------------------------------------------------------------------------

    def mark(self):
        """Everything in the session so far is accounted for (the panel shows it, or it is older than the thread)."""
        messages = self._read()
        if messages is None:
            return False
        with self.lock:               # an empty session is a cursor too: its first message is news, not history
            self.seen = max(self.seen if self.seen is not None else -1, _sequence(messages[-1]) if messages else -1)
        return True

    def poll(self, shown=()):
        """The assistant texts that appeared since the last read, oldest first, each at most once.

        ``shown`` are the answers the panel already displays: a message that is only a piece of one of them is the
        stream's own transcript arriving late, not a follow-up."""
        messages = self._read()
        if messages is None:
            return []
        with self.lock:
            baseline = self.seen
            self.seen = max(baseline if baseline is not None else -1, _sequence(messages[-1]) if messages else -1)
        if baseline is None:                  # the first read only sets the cursor: older history is not news
            return []
        fresh = [m for m in messages if _sequence(m) > baseline]
        if fresh and _sequence(messages[0]) > baseline + 1:
            with self.lock:
                self.counts["gaps"] += 1      # more arrived between two reads than one read holds
        known = [_normal(text) for text in shown if text]
        found = []
        for message in fresh:
            if message.get("role") != "assistant":
                continue
            text = clean_for_panel(_text(message))
            if not text or SILENT.match(text):
                continue
            flat = _normal(text)
            if any(flat in own for own in known):
                continue
            found.append(text)
        if found:
            with self.lock:
                self.counts["followups"] += len(found)
        return found

    def reset(self):
        """The thread was cleared: the next read starts from the session as it is then, not from old messages."""
        with self.lock:
            self.seen = None

    def snapshot(self):
        with self.lock:
            return {"cursor": self.seen, "counts": dict(self.counts), "last_error": self.last_error}
