"""What the observation module tells butler-core (W8, REQ-025; System 3 feedback, REQ-027).

Two kinds of records leave this module, both metadata only: a released
observation (source, span, how many facts of which kinds) as an ingest event
under a stable key, and the owner's signals (a pause, a revoked consent, a
silence request) as feedback events. Records go through a local outbox so a
core that is down loses nothing; the flusher retries every few seconds, which
keeps the "available within 60 seconds" bound (REQ-025) whenever the core is up.
No transcript, frame, fact text or identity ever enters a record.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

FLUSH_INTERVAL = 5.0
OUTBOX_LIMIT = 5000


class CoreLink:
    def __init__(self, core, state_dir, *, switch="observation", clock=time.time, interval=FLUSH_INTERVAL):
        self.core, self.switch, self.clock, self.interval = core, switch, clock, interval
        self.outbox = Path(state_dir) / "core-outbox.jsonl"
        self.lock = threading.Lock()
        self.thread = None
        self.stopped = threading.Event()
        self.sent = self.failed = self.rejected = 0
        self.last_error = None

    # -- switch ---------------------------------------------------------------------------

    def enabled(self):
        try:
            return bool(self.core and self.core.switched(self.switch))
        except Exception:
            return False

    # -- records ----------------------------------------------------------------------------

    def observation_released(self, observation_id, source, start, end, facts):
        """An observation the module committed: span and counts, never the facts themselves."""
        kinds = sorted({str(f.get("kind") or f.get("type") or "fact")[:30] for f in facts if isinstance(f, dict)})
        self._queue({"method": "events.admit", "params": {"event": {
            "source": f"ambient:{source}", "kind": "observation", "occurred_at": float(end), "persona": "butler",
            "principal": "voice:owner", "dedup_key": f"ambient:observation:{observation_id}", "privacy": "raw_local_only",
            "payload": {"source": str(source), "duration_s": round(float(end) - float(start), 1), "facts": len(facts), "kinds": kinds[:12]}}}})

    def feedback(self, kind, detail=None, subject=None):
        """An owner signal (full_pause, resume, consent_revoked, silence_request): the fact and its scope only."""
        clean = {k: v for k, v in (detail or {}).items() if isinstance(v, (str, int, float, bool)) and len(str(v)) <= 80}
        self._queue({"method": "feedback.record", "params": {"kind": str(kind)[:40], "subject": subject, "detail": clean}})

    # -- outbox -----------------------------------------------------------------------------

    def _queue(self, record):
        if not self.enabled():
            return
        record["queued_at"] = self.clock()
        with self.lock:
            self.outbox.parent.mkdir(parents=True, exist_ok=True)
            with self.outbox.open("a") as stream:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        self.start()

    def start(self):
        if self.thread is not None or self.core is None:
            return
        self.thread = threading.Thread(target=self._loop, name="core-link", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopped.set()

    def _loop(self):
        while not self.stopped.is_set():
            self.flush()
            self.stopped.wait(self.interval)

    def flush(self, limit=200):
        """Send queued records in order; stop at the first failure so order and the outbox are kept."""
        with self.lock:
            if not self.outbox.exists():
                return 0
            lines = self.outbox.read_text().splitlines()
        if not lines:
            return 0
        done = 0
        for line in lines[:limit]:
            try:
                record = json.loads(line)
            except ValueError:
                done += 1
                continue
            try:
                self.core.Client(timeout=5.0).call(record["method"], **record["params"])
            except getattr(self.core, "CoreError", ()) as error:
                # A definitive refusal (unknown kind, bad contract) must not block the queue: drop it, count it.
                self.rejected += 1
                self.last_error = f"rejected:{getattr(error, 'code', '')}"
                done += 1
                continue
            except Exception as error:
                self.failed += 1
                self.last_error = type(error).__name__
                break
            done += 1
            self.sent += 1
        if done:
            with self.lock:
                remaining = self.outbox.read_text().splitlines()[done:]
                self.outbox.write_text("\n".join(remaining[-OUTBOX_LIMIT:]) + ("\n" if remaining else ""))
        return done

    def snapshot(self):
        try:
            pending = sum(1 for _ in self.outbox.open()) if self.outbox.exists() else 0
        except OSError:
            pending = None
        return {"enabled": self.enabled(), "pending": pending, "sent": self.sent, "failed": self.failed, "rejected": self.rejected,
                "last_error": self.last_error}
