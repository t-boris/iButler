"""The OpenClaw gateway as seen from outside: channel status, restart, and whether a cron job is running (BUG-016).

Used by the channel watchdog. Everything here shells out to the ``openclaw`` CLI or reads a file the gateway writes;
the watchdog itself only sees three callables, so its decisions are testable without a gateway.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .openclaw import OPENCLAW, run

CRON_STATE = Path.home() / ".openclaw" / "cron" / "jobs-state.json"
STATUS_TIMEOUT = 30
RESTART_TIMEOUT = 120


class GatewayUnavailable(Exception):
    """The gateway did not answer a status request with usable data."""


def _json_of(text):
    """The CLI may print progress lines before the JSON document."""
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON in the output")
    return json.loads(text[start:])


def running_ids(node, name="", found=None):
    """Names of the jobs whose state carries a non-empty ``runningAtMs``; the file's layout is not relied upon."""
    found = [] if found is None else found
    if isinstance(node, dict):
        if node.get("runningAtMs"):
            found.append(name or "?")
        for key, value in node.items():
            # jobs-state.json keeps `jobs[<id>].state.runningAtMs`: the id is the key above `state`.
            running_ids(value, name if key == "state" else str(key), found)
    elif isinstance(node, list):
        for item in node:
            running_ids(item, name, found)
    return found


class GatewayControl:
    def __init__(self, executable=OPENCLAW, runner=subprocess.run, cron_state=CRON_STATE):
        self.executable, self.runner, self.cron_state = executable, runner, Path(cron_state)

    def status(self):
        """``openclaw channels status --json`` as a dict; GatewayUnavailable when it cannot be obtained."""
        try:
            result = run([self.executable, "channels", "status", "--json"], STATUS_TIMEOUT, self.runner)
        except Exception as error:                      # missing binary, timeout
            raise GatewayUnavailable(str(error)) from None
        if result.returncode:
            raise GatewayUnavailable(f"openclaw exit {result.returncode}")
        try:
            return _json_of(result.stdout or "")
        except ValueError:
            raise GatewayUnavailable("status output is not JSON") from None

    def restart(self):
        """``openclaw gateway restart``; never raises, the outcome is the dict."""
        try:
            result = run([self.executable, "gateway", "restart"], RESTART_TIMEOUT, self.runner)
        except Exception as error:
            return {"ok": False, "detail": str(error)[:200]}
        if result.returncode:
            return {"ok": False, "detail": f"openclaw exit {result.returncode}"}
        return {"ok": True, "detail": "restarted"}

    def running_jobs(self):
        """Cron jobs running right now. No state file means no jobs; an unreadable one is reported, not guessed."""
        try:
            text = self.cron_state.read_text()
        except FileNotFoundError:
            return []
        except OSError as error:
            raise GatewayUnavailable(f"cron state unreadable: {error.__class__.__name__}") from None
        try:
            return running_ids(json.loads(text))
        except ValueError:
            raise GatewayUnavailable("cron state is not valid JSON") from None
