"""The nightly Back Burner research cycle as one tracked operation (feature butler-back-burner-researcher).

``lib.backburner start-cycle --id <id>`` in the Butler workspace records the job in
``state/backburner/cycles/<id>/`` and starts the cycle as a detached process of its own; ``track`` follows it to
the ``result.json`` the cycle writes when it ends. The id is part of the operation's parameters, so a retry after a
lost response, reconciliation and recovery after a restart all find the same job and never start a second cycle. A
cycle that disappears without a result is reported as interrupted and is not restarted: the next night's cycle
recovers whatever it left behind. Everything else (identity check, stopping the process group) is the dispatcher's.
"""
from __future__ import annotations

import re

from .base import FAILED, SUCCEEDED, Rejected
from .dispatcher import CANCELLED_EXIT, DispatchAdapter

ONLY = re.compile(r"[^\x00-\x1f\x7f]{1,80}")


class BackburnerAdapter(DispatchAdapter):
    """``backburner.cycle``: every due research folder, one after another (or just ``only``)."""

    name = "backburner"
    key_param = "cycle_id"
    key_pattern = re.compile(r"bb-[0-9]{8}-[0-9]{6}-[a-f0-9]{6}")

    def __init__(self, workspace, python, path_env, extra_env=None, **kwargs):
        super().__init__(workspace, python, path_env, extra_env, **kwargs)
        self.root = self.workspace / "state" / "backburner" / "cycles"

    def marker(self, key):
        return f"-m lib.backburner cycle --id {key}"

    def command(self, operation):
        params = operation["params"]
        command = [self.python, "-m", "lib.backburner", "start-cycle", "--id", self.key(operation)]
        only = params.get("only")
        if only is not None:
            if not isinstance(only, str) or not ONLY.fullmatch(only):
                raise Rejected("invalid folder name")
            command += ["--only", only]
        for flag, name in (("--window-start", "window_start"), ("--window-end", "window_end"), ("--max-runs", "max_runs")):
            if params.get(name) is not None:
                command += [flag, str(params[name])]
        return command, None

    def _result(self, path, result):
        """A finished cycle writes its own summary; only a stop on request is a failure here."""
        if result.get("exit_code") == CANCELLED_EXIT:
            return FAILED, {"cycle_id": path.name, "reason": "stopped on request"}
        runs = result.get("runs") or []
        return SUCCEEDED, {"cycle_id": path.name, "runs": len(runs), "skipped": len(result.get("skipped") or []),
                           "failed_runs": sum(1 for run in runs if run.get("status") == "failed"),
                           "failed_folders": [str(name)[:80] for name in (result.get("failed_folders") or [])][:20]}

    def health(self):
        return {"name": self.name, "version": self.version, "ok": (self.workspace / "lib" / "backburner.py").is_file()}

