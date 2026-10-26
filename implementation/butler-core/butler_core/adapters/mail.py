"""Mail readings and approved replies through the workspace's ``butler-mail`` (feature butler-email-digest-and-approved-replies).

The CLI talks to Mail.app and the local model; the core decides when a reading
runs, records it, delivers the digest, holds the owner's approval for a reply
and reconciles a send whose outcome was lost from the Sent mailbox. Bodies
stay in the workspace's private store; the operation result carries the digest
text (sender, subject, date, summary) and counts.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from .base import DONE, NOT_DONE, UNKNOWN, Adapter, Ambiguous, NotDispatched, Rejected

SCAN_TIMEOUT = 900
REPLY_TIMEOUT = 240
LOOKUP_TIMEOUT = 240
SENT_GRACE = 600               # a sent message may take a moment to land in the Sent mailbox
EXIT_MAIL_APP = 6


class MailAdapter(Adapter):
    name = "mail"
    tracked = False

    def __init__(self, workspace, path_env, extra_env=None, run=subprocess.run, clock=time.time):
        self.workspace = Path(workspace)
        self.cli = self.workspace / "bin/butler-mail"
        self.path_env, self.extra_env = path_env, dict(extra_env or {})
        self.run, self.clock = run, clock

    def environment(self):
        return {"HOME": str(Path.home()), "USER": os.environ.get("USER", ""), "LANG": os.environ.get("LANG", "en_US.UTF-8"),
                "TMPDIR": os.environ.get("TMPDIR", "/tmp"), **self.extra_env, "PATH": self.path_env,
                "PYTHONPATH": str(self.workspace)}

    def _cli(self, arguments, timeout):
        try:
            return self.run([str(self.cli), *arguments], capture_output=True, text=True, timeout=timeout,
                            env=self.environment(), cwd=str(self.workspace))
        except FileNotFoundError:
            raise NotDispatched("butler-mail is not installed") from None
        except subprocess.TimeoutExpired:
            raise Ambiguous(f"butler-mail gave no answer within {timeout}s") from None

    @staticmethod
    def _payload(result):
        try:
            return json.loads(result.stdout)
        except (TypeError, ValueError):
            return None

    # -- execute ------------------------------------------------------------------------

    def execute(self, operation, cancel):
        kind, params = operation["kind"], operation["params"]
        if kind == "mail.scan":
            return self._scan(params)
        if kind == "mail.reply":
            return self._reply(operation, params)
        raise Rejected(f"unknown mail operation {kind}")

    def _scan(self, params):
        arguments = ["scan", "--json", "--slot", str(params.get("slot") or "manual")]
        if params.get("since_ts"):
            arguments += ["--since-ts", str(float(params["since_ts"]))]
        if params.get("lookback_hours"):
            arguments += ["--hours", str(float(params["lookback_hours"]))]
        result = self._cli(arguments, SCAN_TIMEOUT)
        if result.returncode == EXIT_MAIL_APP:
            # Nothing was read; the next scheduled reading covers the gap (REQ-001, REQ-005).
            raise Rejected("Mail.app unavailable: " + (result.stderr or "").strip().splitlines()[-1:][0][:160] if result.stderr else "Mail.app unavailable")
        if result.returncode:
            tail = (result.stderr or result.stdout or "").strip().splitlines()[-1:] or [""]
            raise Rejected(f"butler-mail exit {result.returncode}: {tail[0][:160]}")
        payload = self._payload(result)
        if not isinstance(payload, dict) or not payload.get("reading"):
            raise Ambiguous("butler-mail answered without a reading record")
        keep = {k: payload.get(k) for k in ("reading", "slot", "total", "new", "important", "needs_reply", "stale_hours",
                                            "digest", "offers")}
        return {"receipt": f"mail:reading:{payload['reading']}", "result": keep}

    def _reply(self, operation, params):
        if not params.get("key") or not str(params.get("text") or "").strip():
            raise Rejected("a reply needs the message key and a text")
        arguments = ["reply", str(params["key"]), "--text", str(params["text"]), "--core-operation", operation["id"]]
        if params.get("outgoing_id") and params.get("sha"):
            # A draft edited in Mail.app: send that message, and only while it matches the approved hash (DEC-041).
            arguments += ["--outgoing", str(int(params["outgoing_id"])), "--sha", str(params["sha"])]
        result = self._cli(arguments, REPLY_TIMEOUT)
        if result.returncode == EXIT_MAIL_APP:
            raise NotDispatched("Mail.app unavailable; nothing was sent")
        if result.returncode:
            tail = (result.stderr or result.stdout or "").strip().splitlines()[-1:] or [""]
            if "no message matches" in tail[0] or "ambiguous" in tail[0] or "draft changed since approval" in tail[0]:
                raise Rejected(tail[0][:160])
            raise Ambiguous(f"butler-mail exit {result.returncode}: {tail[0][:160]}")
        payload = self._payload(result)
        if not isinstance(payload, dict) or not payload.get("sent_at"):
            raise Ambiguous("butler-mail answered without a send record")
        return {"receipt": f"mail:sent:{params['key']}",
                "result": {"subject": payload.get("subject"), "sent_at": payload.get("sent_at"), "account": payload.get("account")}}

    # -- reconcile ----------------------------------------------------------------------

    def reconcile(self, operation):
        kind, params = operation["kind"], operation["params"]
        if kind == "mail.scan":
            return UNKNOWN, {"reason": "a reading is repeated by the next schedule, never reconciled"}
        try:
            result = self._cli(["find-sent", str(params["key"]), "--json"], LOOKUP_TIMEOUT)
        except (NotDispatched, Ambiguous) as error:
            return UNKNOWN, {"reason": f"Sent mailbox lookup unavailable: {error.detail}"}
        if result.returncode:
            return UNKNOWN, {"reason": "Sent mailbox lookup failed"}
        payload = self._payload(result) or {}
        if payload.get("sent_at") or payload.get("found"):
            return DONE, {"receipt": f"mail:sent:{params['key']}", "result": {"evidence": payload.get("found") or "store"}}
        if payload.get("started_at") is None:
            return NOT_DONE, {}                       # the send never began
        if (payload.get("age") or 0) < SENT_GRACE:
            return UNKNOWN, {"reason": "send started recently; the Sent mailbox may lag"}
        return NOT_DONE, {}
