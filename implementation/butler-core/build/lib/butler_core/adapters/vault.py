"""Knowledge and vault as core operations (W6; inventory 5, 7, 10, 11, 16; REQ-023).

``vault.write`` applies one write the workspace library already decided on
(its policy, schema, snapshot and audit stay there); the core checks the
persona's source scope, holds the owner's approval for ``confirm`` rules and
reconciles a lost outcome from the vault itself. The write's content lives in
a spec file under the workspace state; the operation carries its path and
checksum, so an approval is bound to exactly that content.

``knowledge.recall`` records a search before it runs: the persona's sources
are checked at the search boundary and the decision is kept. The adapter
itself does nothing but return a receipt.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

from .base import DONE, NOT_DONE, UNKNOWN, Adapter, Ambiguous, NotDispatched, Rejected

APPLY_TIMEOUT = 120
CHECK_TIMEOUT = 60


class VaultAdapter(Adapter):
    name = "vault"
    tracked = False

    def __init__(self, workspace, python, path_env, extra_env=None, run=subprocess.run):
        self.workspace, self.python = Path(workspace), str(python)
        self.path_env, self.extra_env, self.run = path_env, dict(extra_env or {}), run

    def environment(self):
        return {"HOME": str(Path.home()), "USER": os.environ.get("USER", ""), "LANG": os.environ.get("LANG", "en_US.UTF-8"),
                "TMPDIR": os.environ.get("TMPDIR", "/tmp"), **self.extra_env, "PATH": self.path_env,
                "PYTHONPATH": str(self.workspace)}

    def _cli(self, arguments, timeout):
        try:
            return self.run([self.python, "-m", "lib.vault", *arguments], capture_output=True, text=True, timeout=timeout,
                            env=self.environment(), cwd=str(self.workspace))
        except FileNotFoundError:
            raise NotDispatched("workspace python or lib.vault is not installed") from None
        except subprocess.TimeoutExpired:
            raise Ambiguous(f"lib.vault gave no answer within {timeout}s") from None

    @staticmethod
    def _spec(params):
        spec = params.get("spec")
        if not spec or not params.get("sha256"):
            raise Rejected("a vault write names its spec file and checksum")
        return str(spec)

    def execute(self, operation, cancel):
        if operation["kind"] not in ("vault.write", "vault.write_confirmed"):
            raise Rejected(f"unknown vault operation {operation['kind']}")
        params = operation["params"]
        result = self._cli(["core-apply", self._spec(params), params["sha256"], "--operation", operation["id"]], APPLY_TIMEOUT)
        if result.returncode:
            tail = (result.stderr or result.stdout or "").strip().splitlines()[-1:] or [""]
            if any(marker in tail[0] for marker in ("PolicyError", "SchemaError", "VaultPathError", "checksum", "blocked",
                                                     "refused", "not allowed", "no such spec")):
                raise Rejected(tail[0][:200])
            raise Ambiguous(f"lib.vault exit {result.returncode}: {tail[0][:160]}")
        try:
            payload = json.loads(result.stdout)
        except ValueError:
            raise Ambiguous("lib.vault answered without a write record") from None
        if not payload.get("ok"):
            raise Rejected(f"vault refused: {payload.get('action')}")
        return {"receipt": f"vault:{params.get('kind')}:{params.get('rel_path')}@{payload.get('audit_id')}",
                "result": {k: payload.get(k) for k in ("path", "action", "bytes_delta", "snapshot", "audit_id") if k in payload}}

    def reconcile(self, operation):
        params = operation["params"]
        try:
            result = self._cli(["core-check", self._spec(params), params["sha256"]], CHECK_TIMEOUT)
        except (NotDispatched, Ambiguous) as error:
            return UNKNOWN, {"reason": f"vault check unavailable: {error.detail}"}
        if result.returncode:
            return UNKNOWN, {"reason": "vault check failed"}
        try:
            payload = json.loads(result.stdout)
        except ValueError:
            return UNKNOWN, {"reason": "vault check answered without a record"}
        if payload.get("applied"):
            return DONE, {"receipt": f"vault:{params.get('kind')}:{params.get('rel_path')}@reconciled",
                          "result": {"path": params.get("rel_path"), "evidence": "content present"}}
        return NOT_DONE, {}


class KnowledgeAdapter(Adapter):
    """``knowledge.recall``: the record of a search; the search itself runs in the caller after this succeeds."""

    name = "knowledge"
    tracked = False

    def execute(self, operation, cancel):
        params = operation["params"]
        scopes = params.get("scopes")
        if not isinstance(scopes, list) or not scopes or not params.get("query_sha256"):
            raise Rejected("a recall names its scopes and the query checksum")
        return {"receipt": f"recall:{operation['idempotency_key']}",
                "result": {"scopes": [str(s)[:40] for s in scopes][:8], "k": int(params.get("k") or 0)}}

    def reconcile(self, operation):
        return DONE, {"receipt": f"recall:{operation['idempotency_key']}"}
