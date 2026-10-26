"""Tatyana's GitHub delivery as a core operation (W9; inventory 15).

``github.pr`` pushes a feature branch, opens its pull request and arms
auto-merge through the workspace's own ``tetyana-pr`` wrapper, which keeps the
hard invariants (branch pattern, squash auto-merge, no admin override). The
core records it under the branch and head commit, so a repeated request never
opens a second pull request, and a lost answer is settled from GitHub itself:
the pull request of that branch is the evidence.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from .base import DONE, NOT_DONE, UNKNOWN, Adapter, Ambiguous, NotDispatched, Rejected

OPEN_TIMEOUT = 900
LOOKUP_TIMEOUT = 120
REFUSALS = ("is not feature/", "not feature/*", "branch", "invalid", "permission", "protected")


class GitHubAdapter(Adapter):
    name = "github"
    tracked = False

    def __init__(self, workspace, path_env, extra_env=None, run=subprocess.run):
        self.workspace = Path(workspace)
        self.cli = self.workspace / "bin/tetyana-pr"
        self.path_env, self.extra_env, self.run = path_env, dict(extra_env or {}), run

    def environment(self):
        return {"HOME": str(Path.home()), "USER": os.environ.get("USER", ""), "LANG": os.environ.get("LANG", "en_US.UTF-8"),
                "TMPDIR": os.environ.get("TMPDIR", "/tmp"), **self.extra_env, "PATH": self.path_env}

    def _cli(self, arguments, timeout):
        try:
            return self.run([str(self.cli), *arguments], capture_output=True, text=True, timeout=timeout,
                            env=self.environment(), cwd=str(self.workspace))
        except FileNotFoundError:
            raise NotDispatched("tetyana-pr is not installed") from None
        except subprocess.TimeoutExpired:
            raise Ambiguous(f"tetyana-pr gave no answer within {timeout}s") from None

    def execute(self, operation, cancel):
        if operation["kind"] != "github.pr":
            raise Rejected(f"unknown github operation {operation['kind']}")
        params = operation["params"]
        if not params.get("branch") or not params.get("title"):
            raise Rejected("a pull request needs a branch and a title")
        arguments = ["open", "--branch", str(params["branch"]), "--title", str(params["title"]), "--core-operation", operation["id"]]
        if params.get("body_file"):
            arguments += ["--body-file", str(params["body_file"])]
        result = self._cli(arguments, OPEN_TIMEOUT)
        if result.returncode:
            tail = (result.stderr or result.stdout or "").strip().splitlines()[-1:] or [""]
            if any(marker in tail[0] for marker in REFUSALS):
                raise Rejected(tail[0][:200])
            raise Ambiguous(f"tetyana-pr exit {result.returncode}: {tail[0][:160]}")
        try:
            receipt = json.loads(result.stdout)
        except ValueError:
            raise Ambiguous("tetyana-pr answered without a receipt") from None
        if not receipt.get("number"):
            raise Ambiguous("tetyana-pr answered without a pull request number")
        return {"receipt": f"github:pr:{receipt['number']}",
                "result": {k: receipt.get(k) for k in ("number", "url", "state", "branch") if receipt.get(k) is not None}}

    def reconcile(self, operation):
        params = operation["params"]
        try:
            found = self._cli(["find", "--branch", str(params.get("branch") or "")], LOOKUP_TIMEOUT)
        except (NotDispatched, Ambiguous) as error:
            return UNKNOWN, {"reason": f"GitHub lookup unavailable: {error.detail}"}
        if found.returncode:
            return UNKNOWN, {"reason": "GitHub lookup failed"}
        try:
            payload = json.loads(found.stdout) or {}
        except ValueError:
            return UNKNOWN, {"reason": "GitHub lookup answered without a record"}
        if payload.get("number"):
            return DONE, {"receipt": f"github:pr:{payload['number']}",
                          "result": {k: payload.get(k) for k in ("number", "url", "state") if payload.get(k) is not None}}
        return NOT_DONE, {}
