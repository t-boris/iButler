"""Export-only operations (W5; inventory 2): the record of data leaving the machine.

Narration text sent to a speech service and a voice note sent for transcription
are exports, not effects: nothing has to be undone or reconciled afterwards.
The operation exists so the export policy is checked and the decision recorded
on the one operation path before the program makes the call itself. The
adapter therefore does nothing but return a receipt; the caller proceeds only
when the operation succeeded.
"""
from __future__ import annotations

from .base import DONE, Adapter, Rejected

KINDS = {"export.narration.openai": "narration-text", "export.narration.edge": "narration-text",
         "export.voice-note": "voice-note"}


class ExportAdapter(Adapter):
    name = "export"
    tracked = False

    def execute(self, operation, cancel):
        kind = operation["kind"]
        if kind not in KINDS:
            raise Rejected(f"unknown export {kind}")
        params = operation["params"]
        size = params.get("chars") if "narration" in kind else params.get("bytes")
        if not isinstance(size, (int, float)) or size <= 0:
            raise Rejected("an export names the size of what leaves: chars or bytes")
        return {"receipt": f"export:{KINDS[kind]}:{operation['idempotency_key']}",
                "result": {"category": KINDS[kind], "size": int(size)}}

    def reconcile(self, operation):
        # Recording the decision is the whole effect; a lost response cannot have exported anything twice.
        return DONE, {"receipt": f"export:{KINDS.get(operation['kind'], 'unknown')}:{operation['idempotency_key']}"}
