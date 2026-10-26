"""Local JSON-RPC over a 0600 Unix socket, with the caller classified by its process.

All processes run as the same Unix user, so the socket permission alone cannot
tell the owner from an agent. The server therefore classifies each peer by
its process (macOS ``LOCAL_PEERPID``):

* ``gateway``  — the OpenClaw gateway process itself (the in-process bridge plugin);
* ``ambient``  — the ambient-awareness service process;
* ``terminal`` — a process with a controlling terminal and no agent ancestor;
* ``local``    — anything else, including agent tools and dispatched work.

Owner-only methods (approvals, resolutions, configuration activation) are not
available to ``local`` callers; owners approve from Telegram (through the
bridge), by verified voice (ambient) or from a real terminal. See finding
F-001 for the remaining same-user limitation.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import threading
import time
import traceback
from pathlib import Path

from .approvals import ApprovalError
from .config import ConfigError
from .diagnostics import redact
from .feedback import FeedbackError
from .operations import OperationError
from .store import StorageExhausted
from .tasks import TaskError

SOL_LOCAL, LOCAL_PEERPID = 0, 2
REQUEST_LIMIT = 1024 * 1024
GATEWAY = re.compile(r"/openclaw/dist/index\.js gateway\b")
AMBIENT = re.compile(r"-m butler_ambient\.cli --state ")
AGENT = re.compile(r"(/openclaw/|\bacpx\b|\bcodex\b|/claude\b|\bclaude-code\b|lib/dispatch\.py|\bbutler_ambient\.work_tasks\b)")
TERMINAL_PRINCIPAL = "terminal:owner"
VOICE_PRINCIPAL = "voice:owner"


class NotFound(Exception):
    """The requested record does not exist; reported as code ``not_found``."""


def process(pid):
    try:
        out = subprocess.run(["/bin/ps", "-o", "ppid=,tty=,command=", "-p", str(pid)], capture_output=True,
                             text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out:
        return None
    parts = out.split(None, 2)
    if len(parts) < 3:
        return None
    return {"ppid": int(parts[0]), "tty": parts[1], "command": parts[2]}


def classify_peer(pid):
    info = process(pid)
    if info is None:
        return "local"
    if GATEWAY.search(info["command"]):
        return "gateway"
    if AMBIENT.search(info["command"]):
        return "ambient"
    if info["tty"] in ("??", "-", ""):
        return "local"
    parent, depth = info["ppid"], 0
    while parent > 1 and depth < 30:
        ancestor = process(parent)
        if ancestor is None:
            break
        if AGENT.search(ancestor["command"]) or GATEWAY.search(ancestor["command"]):
            return "local"
        parent, depth = ancestor["ppid"], depth + 1
    return "terminal"


ALL = ("gateway", "ambient", "terminal", "local")
OWNER = ("terminal", "ambient")


class Server:
    def __init__(self, core, path, classify=classify_peer, backup=None, cron=None):
        self.core, self.path, self.classify = core, Path(path), classify
        self.backup, self.cron = backup, cron
        self.sock = None
        self.stopping = threading.Event()
        self.methods = {
            "ping": (self.ping, ALL),
            "doctor": (self.doctor, ALL),
            "bridge.hello": (lambda p, c: core.bridge_hello(p), ("gateway",)),
            "bridge.config": (lambda p, c: core.bridge_poll(), ("gateway",)),
            "bridge.admit": (lambda p, c: core.admit_message(p), ("gateway",)),
            "bridge.turn_started": (lambda p, c: core.turn_started(p), ("gateway",)),
            "bridge.turn_ended": (lambda p, c: core.turn_ended(p), ("gateway",)),
            "bridge.reply": (lambda p, c: core.reply_observed(p), ("gateway",)),
            "bridge.metric": (self.bridge_metric, ("gateway",)),
            "submit": (self.submit, ALL),
            "execute": (self.execute, ALL),
            "operation.get": (lambda p, c: self.operation(p), ALL),
            "operation.list": (lambda p, c: core.operations.list(p.get("states"), int(p.get("limit", 50)),
                                                                 p.get("persona")), ALL),
            "task.get": (lambda p, c: core.status(p["task"]), ALL),
            "task.list": (lambda p, c: core.status(limit=int(p.get("limit", 20)), persona=p.get("persona")), ALL),
            "cancel": (lambda p, c: core.cancel(p["operation"], c), ALL),
            "approve": (self.approve, OWNER),
            "reject": (lambda p, c: core.reject(p["operation"], self.principal(c), c), OWNER),
            "resolve": (lambda p, c: core.resolve(p["operation"], p["outcome"], p.get("evidence"),
                                                  self.principal(c), c), OWNER),
            "feedback.record": (lambda p, c: core.signal(p, c), ("ambient", "terminal", "gateway")),
            "feedback.list": (lambda p, c: core.feedback.list(p.get("since"), p.get("kinds"), p.get("persona"),
                                                              int(p.get("limit", 200))), ALL),
            "revoke": (lambda p, c: core.approvals.revoke(p["operation"], self.principal(c)), OWNER),
            "deliver": (self.deliver, ALL),
            "lessons.list": (lambda p, c: core.lessons.list(p.get("status"), p.get("persona"), int(p.get("limit", 50))), ALL),
            "lessons.get": (lambda p, c: core.lessons.get(p["lesson"]), ALL),
            "lessons.set_status": (lambda p, c: core.lessons.set_status(p["lesson"], p["status"], self.principal(c), p.get("text")), OWNER),
            "lessons.answer": (lambda p, c: core.lessons.answer(p["lesson"], p["text"], self.principal(c)), ("terminal", "ambient", "gateway")),
            "lessons.analyse": (lambda p, c: core.lessons.analyse(), ("terminal",)),
            "notify": (self.notify, ALL),
            "delivery.list": (lambda p, c: core.delivery.list(p.get("states"), int(p.get("limit", 50))), ALL),
            "delivery.import": (lambda p, c: core.delivery.import_uncertain(p["entries"]), ("terminal",)),
            "events.admit": (lambda p, c: core.events.admit(p["event"]), ALL),
            "ingest.admit": (lambda p, c: core.ingest.admit(p["event"]), ALL),
            "config.active": (lambda p, c: self.config_active(), ALL),
            "config.versions": (lambda p, c: core.config.versions(), ALL),
            "config.propose": (lambda p, c: core.config.propose(p["content"], f"{c}:{p.get('author', 'unknown')}"),
                               ALL),
            "config.preview": (lambda p, c: core.config.preview(int(p["version"])), ALL),
            "config.activate": (lambda p, c: core.config.activate(int(p["version"]), self.principal(c)),
                                ("terminal",)),
            "config.rollback": (lambda p, c: core.config.rollback(self.principal(c)), ("terminal",)),
            "trace": (lambda p, c: core.diagnostics.trace(p["correlation"]), ALL),
            "metrics": (lambda p, c: [core.diagnostics.summary(n, p.get("since")) for n in p.get("names", [])], ALL),
            "backup.run": (lambda p, c: self.backup.run(), ALL),
            "backup.status": (lambda p, c: self.backup.status(), ALL),
            "cron.reconcile": (lambda p, c: self.cron.reconcile(), ALL),
        }

    @staticmethod
    def principal(caller):
        return {"terminal": TERMINAL_PRINCIPAL, "ambient": VOICE_PRINCIPAL}.get(caller, f"{caller}:unknown")

    # -- handlers -------------------------------------------------------------------

    def ping(self, params, caller):
        return {"ok": True, "caller": caller, "at": time.time()}

    def doctor(self, params, caller):
        return redact(self.core.doctor())

    def bridge_metric(self, params, caller):
        self.core.diagnostics.observe(params["name"], float(params["value_ms"]), params.get("labels"))
        self.core.bridge_seen()
        return {"ok": True}

    def _source(self, params, caller):
        request = params.get("request") or {}
        if caller == "gateway":
            return "bridge"
        if caller == "ambient":
            return "ambient"
        principal = str(request.get("principal") or "")
        if principal.startswith("agent:"):
            return "system2"
        if params.get("source") == "rules" and caller == "terminal":
            return "rules"
        return "cli"

    def submit(self, params, caller):
        return self.core.submit(params["request"], self._source(params, caller), params.get("correlation"))

    def execute(self, params, caller):
        result = self.submit(params, caller)
        if not result.get("id") or result.get("state") not in ("requested", "dispatching"):
            return result
        self.core.wait(result["id"], min(float(params.get("timeout", 60)), 600))
        return self.operation({"operation": result["id"]})

    def operation(self, params):
        from .operations import summary
        if "idempotency_key" in params:
            row = self.core.operations.by_key(str(params["idempotency_key"]))
            if row is None:
                raise NotFound("no operation is recorded under this key")
            return summary(row, code=True)
        return summary(self.core.operations.get(params["operation"]), code=True)

    def approve(self, params, caller):
        record = self.core.approvals.grant(params["operation"], params["code"], self.principal(caller), caller)
        self.core.wake()
        return {"approval": record["id"], "operation": params["operation"], "expires_at": record["expires_at"]}

    def deliver(self, params, caller):
        return self.core.delivery.enqueue(**{k: params.get(k) for k in (
            "recipient", "channel", "persona", "kind", "content", "idempotency_key", "task", "operation",
            "correlation")})

    def notify(self, params, caller):
        """A notice to the owner of ``persona`` from any local program (butler-notify, dispatch, the tasks board)."""
        attachment = params.get("attachment")
        if attachment is not None and not (isinstance(attachment, dict) and attachment.get("path")):
            raise ValueError("attachment needs a path")
        return self.core.notify(params.get("persona") or "butler", str(params.get("text") or ""),
                                str(params.get("kind") or "status"), str(params["idempotency_key"]),
                                task=params.get("task"), operation=params.get("operation"),
                                correlation=params.get("correlation"), attachment=attachment)

    def config_active(self):
        version, content = self.core.config.active()
        return {"version": version, "content": redact(content)}

    # -- transport --------------------------------------------------------------------

    def bind(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.exists() or self.path.is_symlink():
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.settimeout(0.5)
                probe.connect(str(self.path))
                probe.close()
                raise RuntimeError("another core daemon is already serving this socket")
            except (ConnectionRefusedError, FileNotFoundError, socket.timeout):
                self.path.unlink()
            finally:
                probe.close()
        old = os.umask(0o177)
        try:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            self.sock.bind(str(self.path))
        finally:
            os.umask(old)
        self.path.chmod(0o600)
        self.sock.listen(64)

    def serve_forever(self):
        while not self.stopping.is_set():
            try:
                connection, _ = self.sock.accept()
            except OSError:
                if self.stopping.is_set():
                    break
                continue
            threading.Thread(target=self.handle, args=(connection,), daemon=True, name="rpc").start()

    def start(self):
        self.bind()
        threading.Thread(target=self.serve_forever, daemon=True, name="rpc-accept").start()

    def stop(self):
        self.stopping.set()
        if self.sock is not None:
            self.sock.close()
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass

    def handle(self, connection):
        try:
            try:
                pid = connection.getsockopt(SOL_LOCAL, LOCAL_PEERPID)
            except OSError:
                pid = None
            caller = self.classify(pid) if pid else "local"
            connection.settimeout(120)
            stream = connection.makefile("rwb")
            while True:
                line = stream.readline(REQUEST_LIMIT + 1)
                if not line:
                    break
                response = self.dispatch(line, caller)
                stream.write((json.dumps(response, ensure_ascii=False) + "\n").encode())
                stream.flush()
        except (OSError, ValueError):
            pass
        finally:
            connection.close()

    def dispatch(self, line, caller):
        request_id = None
        try:
            if len(line) > REQUEST_LIMIT:
                raise ValueError("request too large")
            request = json.loads(line)
            request_id = request.get("id")
            method = request.get("method")
            params = request.get("params") or {}
            if method not in self.methods or not isinstance(params, dict):
                return {"id": request_id, "ok": False, "error": {"code": "unknown_method", "message": str(method)}}
            handler, allowed = self.methods[method]
            if caller not in allowed:
                return {"id": request_id, "ok": False,
                        "error": {"code": "forbidden", "message": f"{method} is not available to {caller} callers"}}
            return {"id": request_id, "ok": True, "result": handler(params, caller)}
        except StorageExhausted as error:
            return {"id": request_id, "ok": False, "error": {"code": "storage_exhausted", "message": str(error)}}
        except NotFound as error:
            return {"id": request_id, "ok": False, "error": {"code": "not_found", "message": str(error)}}
        except (OperationError, TaskError, ApprovalError, ConfigError, FeedbackError, KeyError, TypeError,
                ValueError) as error:
            return {"id": request_id, "ok": False, "error": {"code": type(error).__name__, "message": str(error)[:500]}}
        except Exception as error:
            traceback.print_exc()
            return {"id": request_id, "ok": False, "error": {"code": "internal", "message": type(error).__name__}}
