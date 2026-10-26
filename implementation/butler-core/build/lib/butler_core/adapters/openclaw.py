"""OpenClaw adapters: Telegram delivery through the gateway and explicit agent turns.

Both call the ``openclaw`` CLI, which talks to the running gateway. The CLI
does not say whether its request reached the gateway, so only failures that
happen before the gateway handshake completes count as "not dispatched";
everything else is ambiguous and is never retransmitted blindly.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import telegram
from .base import UNKNOWN, Adapter, Ambiguous, NotDispatched, Rejected

OPENCLAW = "/opt/homebrew/bin/openclaw"
NODE_BIN = str(Path.home() / ".nvm/versions/node/v22.22.3/bin")
LOCKS = Path.home() / ".openclaw" / "tmp" / f"openclaw-{os.getuid()}"
PRE_HANDSHAKE = re.compile(r"ECONNREFUSED|ENOENT|gateway connect failed|gateway closed before ready|"
                           r"connect ECONN|not yet ready|gateway is not running", re.I)
REJECTED = re.compile(r"chat not found|bot was blocked|forbidden|unknown account|invalid target|"
                      r"message is too long|Bad Request", re.I)


def environment():
    env = dict(os.environ)
    env["PATH"] = f"{NODE_BIN}:" + env.get("PATH", "/usr/bin:/bin")
    return env


def gateway_instance(directory=LOCKS):
    """Identity of the running gateway process: lock file pid and creation time (changes on every restart)."""
    for lock in sorted(Path(directory).glob("gateway.*.lock")):
        try:
            data = json.loads(lock.read_text())
        except (OSError, ValueError):
            continue
        if data.get("pid") and data.get("createdAt"):
            return f"{data['pid']}@{data['createdAt']}"
    return None


def run(command, timeout, runner=subprocess.run):
    try:
        result = runner(command, capture_output=True, text=True, timeout=timeout, env=environment())
    except FileNotFoundError:
        raise NotDispatched("openclaw executable not found") from None
    except subprocess.TimeoutExpired:
        raise Ambiguous(f"openclaw timed out after {timeout}s") from None
    return result


def classify_failure(result):
    text = (result.stderr or "") + "\n" + (result.stdout or "")
    if PRE_HANDSHAKE.search(text):
        raise NotDispatched("gateway unreachable before handshake")
    match = REJECTED.search(text)
    if match:
        raise Rejected(match.group(0)[:80])
    raise Ambiguous(f"openclaw exit {result.returncode}")


def receipt_of(stdout):
    try:
        data = json.loads(stdout)
    except (TypeError, ValueError):
        return None
    for holder in (data, data.get("payload") if isinstance(data, dict) else None,
                   data.get("result") if isinstance(data, dict) else None):
        if isinstance(holder, dict):
            value = holder.get("messageId", holder.get("message_id"))
            if value:
                return str(value)
    return None


class TelegramTransport:
    """Delivery transport for one Telegram bot account (channel ``telegram:<account>``).

    The gateway does not deduplicate a repeated send, so ``deduplicates`` is
    False: any ambiguous attempt becomes ``uncertain`` and is not repeated.
    """

    deduplicates = False

    def __init__(self, account, executable=OPENCLAW, timeout=45, runner=subprocess.run, instance=gateway_instance,
                 token=telegram.bot_token, document=telegram.send_document):
        self.account, self.executable, self.timeout = account, executable, timeout
        self.runner, self._instance = runner, instance
        self.token, self.document = token, document

    def instance(self):
        return self._instance()

    def send(self, message):
        recipient = str(message["recipient"])
        if not recipient.startswith("telegram:"):
            raise Rejected("recipient is not a Telegram chat")
        if message.get("attachment"):
            return self.send_document(message, recipient.split(":", 1)[1])
        command = [self.executable, "message", "send", "--channel", "telegram", "--account", self.account,
                   "--target", recipient.split(":", 1)[1], "--message", message["content"], "--json"]
        if message.get("silent"):
            command.append("--silent")
        result = run(command, self.timeout, self.runner)
        if result.returncode:
            classify_failure(result)
        receipt = receipt_of(result.stdout)
        if receipt is None:
            raise Ambiguous("no message id in the gateway response")
        return receipt

    def send_document(self, message, chat):
        """A file goes straight to the Bot API (DEC-063); the token lives only in this frame."""
        attachment = message["attachment"]
        try:
            token = self.token(self.account)
        except telegram.TokenUnavailable as error:
            raise NotDispatched(str(error)) from None
        return self.document(token, chat, attachment["path"], name=attachment.get("name"),
                             mime=attachment.get("mime"), caption=message.get("content", ""),
                             silent=bool(message.get("silent")), timeout=self.timeout)


class AgentTurnAdapter(Adapter):
    """``openclaw.agent_turn``: one explicitly requested agent turn in an existing chat session.

    Used only for an owner's explicit continuation of an interrupted chat task.
    The turn's reply is delivered by the gateway itself. A lost response cannot
    be reconciled from outside, so the outcome is unknown and the owner decides.
    """

    name = "openclaw"
    tracked = False

    def __init__(self, executable=OPENCLAW, timeout=600, runner=subprocess.run):
        self.executable, self.timeout, self.runner = executable, timeout, runner

    def execute(self, operation, cancel):
        params = operation["params"]
        session = params.get("session")
        if not session or not str(session).startswith("agent:"):
            raise Rejected("chat session unknown")
        command = [self.executable, "agent", "--session-key", session, "--message", params.get("message", ""),
                   "--timeout", str(int(params.get("timeout", self.timeout))), "--deliver", "--json"]
        result = run(command, self.timeout + 30, self.runner)
        if result.returncode:
            classify_failure(result)
        try:
            data = json.loads(result.stdout)
        except ValueError:
            data = {}
        run_id = None
        if isinstance(data, dict):
            run_id = data.get("runId") or (data.get("result") or {}).get("runId")
        return {"receipt": run_id or f"turn@{int(time.time())}", "result": {"run": run_id}}

    def reconcile(self, operation):
        return UNKNOWN, {}

    def health(self):
        return {"ok": Path(self.executable).exists()}

