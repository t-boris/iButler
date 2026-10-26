"""Scripted adapter for tests and failure injection; it has no external effects."""
from __future__ import annotations

import threading

from .base import DONE, NOT_DONE, RUNNING, SUCCEEDED, UNKNOWN, Adapter, Ambiguous, NotDispatched, Rejected


class ScriptedAdapter(Adapter):
    """Each execute() pops the next behaviour from ``script`` (default ``ok``).

    Behaviours: ok, not_dispatched, rejected, ambiguous (effect happened, response
    lost), ambiguous_none (no effect, response lost), stall (waits for cancel),
    crash (effect happened, then the process dies — raises SystemExit).
    """

    def __init__(self, name="test", script=None, reconcile_supported=True, tracked=False):
        self.name, self.script = name, list(script or [])
        self.reconcile_supported, self.tracked = reconcile_supported, tracked
        self.effects = {}          # idempotency key -> number of real effects
        self.calls = []
        self.progress = {}         # idempotency key -> tracked state
        self.lock = threading.Lock()
        self.started = threading.Event()

    def _effect(self, key):
        with self.lock:
            self.effects[key] = self.effects.get(key, 0) + 1

    def execute(self, operation, cancel):
        key = operation["idempotency_key"]
        with self.lock:
            behaviour = self.script.pop(0) if self.script else "ok"
            self.calls.append((key, behaviour))
        self.started.set()
        if behaviour == "not_dispatched":
            raise NotDispatched("connection refused before sending")
        if behaviour == "rejected":
            raise Rejected("invalid request")
        if behaviour == "ambiguous":
            self._effect(key)
            raise Ambiguous("response lost")
        if behaviour == "ambiguous_none":
            raise Ambiguous("response lost")
        if behaviour == "stall":
            cancel.wait(30)
            raise Ambiguous("cancelled while waiting")
        if behaviour == "crash":
            self._effect(key)
            raise SystemExit("simulated crash after effect")
        self._effect(key)
        if self.tracked:
            self.progress[key] = RUNNING
        return {"receipt": f"{self.name}:{key}", "result": {"ok": True}}

    def reconcile(self, operation):
        if not self.reconcile_supported:
            return UNKNOWN, {"reason": "no external lookup"}
        key = operation["idempotency_key"]
        if self.effects.get(key):
            return DONE, {"receipt": f"{self.name}:{key}", "found": True}
        return NOT_DONE, {"found": False}

    def track(self, operation):
        return self.progress.get(operation["idempotency_key"], SUCCEEDED), {}

    def cancel(self, operation):
        key = operation["idempotency_key"]
        if self.progress.get(key) == RUNNING:
            self.progress[key] = "failed"
            return {"stopped": True, "effect": "none"}
        return {"stopped": False, "effect": "done" if self.effects.get(key) else "none"}
