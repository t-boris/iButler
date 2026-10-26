"""The adapter contract every external integration implements (REQ-021, REQ-022).

``execute`` performs one external effect for one operation. Its failure type
tells the core what is known about the effect:

* ``NotDispatched`` — the effect certainly did not happen; a bounded retry is safe.
* ``Rejected`` — the external system definitively refused; no effect, no retry.
* any other exception (including ``Ambiguous``) — the outcome is unknown; the
  core reconciles before anything is repeated.

``reconcile`` answers ``("done" | "not_done" | "unknown", evidence)`` from the
external system's own state. Adapters that cannot verify return ``unknown``,
which blocks repetition until the owner resolves the operation.
"""
from __future__ import annotations


class AdapterError(Exception):
    """Base class. ``detail`` must never contain secrets or private content."""

    def __init__(self, detail=""):
        super().__init__(detail)
        self.detail = str(detail)[:300]


class NotDispatched(AdapterError):
    pass


class Rejected(AdapterError):
    pass


class Ambiguous(AdapterError):
    pass


DONE, NOT_DONE, UNKNOWN = "done", "not_done", "unknown"
RUNNING, SUCCEEDED, FAILED, LOST = "running", "succeeded", "failed", "lost"


class Adapter:
    """Override ``execute``; override ``reconcile``, ``track`` and ``cancel`` when supported."""

    name = "adapter"
    version = "1"
    # True when execute() only starts long-running external work that track() follows.
    tracked = False

    def execute(self, operation, cancel):
        """Return ``{"receipt": str, "result": dict}``. ``cancel`` is a threading.Event."""
        raise NotImplementedError

    def reconcile(self, operation):
        return UNKNOWN, {"reason": "reconciliation not supported"}

    def track(self, operation):
        """For tracked adapters: ``(running|succeeded|failed|lost, result)``.

        A ``reason`` key in the result becomes the operation's reason."""
        return SUCCEEDED, {}

    def cancel(self, operation):
        """Stop external work: ``{"stopped": bool, "effect": "none"|"partial"|"done"|"unknown"}``.

        ``partial`` means long-running work was stopped midway and may have left
        partial changes; the operation ends cancelled and says so."""
        return {"stopped": False, "effect": "unknown"}

    def health(self):
        return {"name": self.name, "version": self.version, "ok": True}
