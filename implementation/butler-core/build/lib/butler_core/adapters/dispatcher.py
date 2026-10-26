"""Coding-agent dispatches as tracked operations (W2; inventory 4 and 17).

The Butler dispatcher keeps each job in ``state/dispatches/<id>/`` (meta.json,
result.json, logs). The caller chooses the id and it is part of the operation's
parameters, so a retry after a lost response, reconciliation and recovery after
a restart all look for exactly that directory and never start a second worker.

``execute`` only starts the detached worker; ``track`` follows it to its result.
A worker that disappears without a result is reported as interrupted and is
never restarted automatically. Stopping a worker kills only a process whose
command line still names this job, so a reused process id is never signalled.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path

from .base import DONE, FAILED, NOT_DONE, RUNNING, SUCCEEDED, Adapter, Ambiguous, NotDispatched, Rejected

LAUNCH_TIMEOUT = 60
# A worker that has not recorded its process id this long after start is treated as lost.
START_GRACE = 120
STOP_WAIT = 5.0
CANCELLED_EXIT = -15


def _read(path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _write_private(path, value):
    temporary = path.with_name(f".{path.name}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w") as handle:
        json.dump(value, handle)
    os.replace(temporary, path)


class DispatchAdapter(Adapter):
    """``dispatch.<backend>``: ``lib.dispatch start --id <id>`` in the Butler workspace."""

    name = "dispatcher"
    version = "1"
    tracked = True
    key_param = "dispatch_id"
    key_pattern = re.compile(r"[0-9]{8}-[0-9]{6}-[a-f0-9]{6}")

    def __init__(self, workspace, python, path_env, extra_env=None, run=subprocess.run, clock=time.time):
        # Resolved, because worker command lines name the resolved path and identity checks compare them.
        self.workspace, self.python, self.path_env = Path(workspace).resolve(), str(python), path_env
        self.root = self.workspace / "state" / "dispatches"
        self.extra_env = dict(extra_env or {})
        self.run, self.clock = run, clock

    # -- identity -------------------------------------------------------------------

    def key(self, operation):
        key = (operation.get("params") or {}).get(self.key_param)
        if not isinstance(key, str) or not self.key_pattern.fullmatch(key):
            raise Rejected("invalid dispatch id")
        return key

    def directory(self, operation):
        path = self.root / self.key(operation)
        if path.is_symlink():
            raise Rejected("dispatch directory is a symlink")
        return path

    def marker(self, key):
        return f"-m lib.dispatch _monitor {key}"

    def worker_pid(self, path):
        return (_read(path / "meta.json") or {}).get("monitor_pid")

    # -- launching ----------------------------------------------------------------

    def environment(self, operation):
        params = operation["params"]
        # Never the core's own environment: workers get no credentials, only what they need to run.
        env = {"HOME": str(Path.home()), "USER": os.environ.get("USER", ""), "LOGNAME": os.environ.get("LOGNAME", ""),
               "LANG": os.environ.get("LANG", "en_US.UTF-8"), "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
               **self.extra_env,
               "PATH": self.path_env, "PYTHONPATH": str(self.workspace), "BUTLER_CORE_OPERATION": operation["id"],
               "BUTLER_CHAIN_DEPTH": str(int(params.get("chain_depth", 0)))}
        return {k: v for k, v in env.items() if v}

    def command(self, operation):
        params = operation["params"]
        command = [self.python, "-m", "lib.dispatch", "start", params["task_kind"], "--project", params["project"],
                   "--prompt", params["prompt"], "--id", self.key(operation)]
        for flag in ("backend", "model", "timeout"):
            if params.get(flag) is not None:
                command += [f"--{flag}", str(params[flag])]
        return command, None

    def execute(self, operation, cancel):
        path = self.directory(operation)
        if (path / "meta.json").exists():
            # A retry after a lost response: the worker already exists.
            return {"receipt": path.name, "result": {"dispatch_id": path.name}}
        command, stdin = self.command(operation)
        try:
            done = self.run(command, cwd=str(self.workspace), env=self.environment(operation), input=stdin,
                            capture_output=True, text=True, timeout=LAUNCH_TIMEOUT)
        except FileNotFoundError:
            raise NotDispatched("dispatcher interpreter not found") from None
        except subprocess.TimeoutExpired:
            raise Ambiguous("dispatcher start timed out") from None
        if done.returncode != 0:
            if (path / "meta.json").exists():
                raise Ambiguous(f"dispatcher start exited {done.returncode} after creating the job")
            # Validation errors name the kind, project or route; never the prompt.
            lines = [line for line in (done.stderr or "").strip().splitlines() if line.strip()]
            raise Rejected((lines[-1] if lines else f"dispatcher start exited {done.returncode}")[:200])
        return {"receipt": path.name, "result": {"dispatch_id": path.name}}

    def reconcile(self, operation):
        path = self.directory(operation)
        if (path / "meta.json").exists():
            return DONE, {"receipt": path.name, "result": {"dispatch_id": path.name}}
        return NOT_DONE, {"reason": "no dispatch directory for this id"}

    # -- following --------------------------------------------------------------------

    def alive(self, pid, key):
        """True only for a live process whose command line still names this job."""
        if not isinstance(pid, int) or pid <= 1:
            return False
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return False
        try:
            shown = self.run(["/bin/ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True,
                             timeout=2).stdout
        except (OSError, subprocess.SubprocessError):
            return True  # cannot check identity; do not declare a possibly live worker lost
        return self.marker(key) in shown

    def _result(self, path, result):
        code = result.get("exit_code")
        summary = {"dispatch_id": path.name, "exit_code": code}
        for field in ("duration_sec", "estimated_cost_usd"):
            if field in result:
                summary[field] = result[field]
        if code == 0:
            return SUCCEEDED, summary
        if code == CANCELLED_EXIT:
            return FAILED, summary | {"reason": "stopped on request"}
        return FAILED, summary | {"reason": f"worker exited {code}"}

    def track(self, operation):
        path = self.directory(operation)
        result = _read(path / "result.json")
        if result is not None:
            return self._result(path, result)
        meta = _read(path / "meta.json") or {}
        pid = self.worker_pid(path)
        if pid and self.alive(pid, path.name):
            return RUNNING, {}
        if not pid and self.clock() - float(meta.get("started_at_ts") or 0) < START_GRACE:
            return RUNNING, {}
        result = _read(path / "result.json")  # the worker may have finished meanwhile
        if result is not None:
            return self._result(path, result)
        return FAILED, {"dispatch_id": path.name, "interrupted": True,
                        "reason": "interrupted: the worker ended without a result"}

    def cancel(self, operation):
        path = self.directory(operation)
        if (path / "result.json").exists():
            return {"stopped": False, "effect": "done"}
        pid = self.worker_pid(path)
        if not pid or not self.alive(pid, path.name):
            return {"stopped": False, "effect": "partial"}
        try:
            if os.getpgid(pid) != pid:
                return {"stopped": False, "effect": "unknown"}
            os.killpg(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return {"stopped": False, "effect": "partial"}
        deadline = time.monotonic() + STOP_WAIT
        while time.monotonic() < deadline and self.alive(pid, path.name):
            time.sleep(0.2)
        if not (path / "result.json").exists():
            started = float((_read(path / "meta.json") or {}).get("started_at_ts") or self.clock())
            _write_private(path / "result.json", {"exit_code": CANCELLED_EXIT,
                                                  "duration_sec": round(self.clock() - started, 2),
                                                  "finished_at_ts": int(self.clock()), "cancelled_by": "butler-core"})
        return {"stopped": True, "effect": "partial"}

    def health(self):
        return {"name": self.name, "version": self.version, "ok": (self.workspace / "lib" / "dispatch.py").is_file()}


class VoiceTaskAdapter(DispatchAdapter):
    """``voice.<backend>``: owner-reviewed voice jobs started by the ambient package's launcher."""

    name = "voice-tasks"
    key_param = "voice_id"
    key_pattern = re.compile(r"voice-[0-9]{8}-[0-9]{6}-[a-f0-9]{10}")

    def __init__(self, workspace, python, path_env, ambient_python, **kwargs):
        super().__init__(workspace, python, path_env, **kwargs)
        self.ambient_python = str(ambient_python)

    def marker(self, key):
        return f"-m butler_ambient.work_tasks worker {self.workspace} {key}"

    def worker_pid(self, path):
        return (_read(path / "voice-launch.json") or {}).get("pid")

    def environment(self, operation):
        env = super().environment(operation)
        env.pop("PYTHONPATH", None)  # the ambient interpreter imports its installed package
        return env

    def command(self, operation):
        params = operation["params"]
        reviewed = {"project": params["project"], "task": params["task"], "kind": params["task_kind"],
                    "route": {"backend": params["backend"], "model": params["model"]}, "timeout": params["timeout"]}
        return ([self.ambient_python, "-m", "butler_ambient.work_tasks", "launch", str(self.workspace),
                 self.key(operation), operation["id"]], json.dumps(reviewed, ensure_ascii=False))
