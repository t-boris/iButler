"""Shared fixtures: a core on a temporary root with test adapters and a fake transport."""
from __future__ import annotations

import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from butler_core.adapters.base import DONE, NOT_DONE, Adapter, Ambiguous, NotDispatched
from butler_core.adapters.testing import ScriptedAdapter
from butler_core.service import Core

OWNER = "telegram:1000001"
TANYA = "telegram:2000002"


def base_config(**overrides):
    content = {
        "schema": 1,
        "owner": {"principals": [OWNER, "terminal:owner", "voice:owner"],
                  "notify": {"channel": "telegram:butler", "recipient": OWNER}},
        "personas": {
            "butler": {"principals": [OWNER], "sources": ["vault", "memory"], "actions": ["test.write", "test.read",
                                                                                           "test.gated",
                                                                                           "test.export",
                                                                                           "openclaw.agent_turn"]},
            "tetyana-dyck": {"principals": [TANYA], "sources": ["tetyana-repo"], "actions": ["test.write"]},
        },
        "grants": [],
        "exports": [{"persona": "butler", "category": "task-prompt", "recipient": "openai"}],
        "operations": {
            "test.write": {"adapter": "test", "approval": "none", "max_attempts": 3, "timeout": 5},
            "test.read": {"adapter": "test", "approval": "none"},
            "test.gated": {"adapter": "test", "approval": "required"},
            "test.export": {"adapter": "test", "export": [{"category": "task-prompt", "recipient": "openai"}]},
            "openclaw.agent_turn": {"adapter": "test"},
        },
        "scenarios": {"chat": {"enabled": True}},
        "delivery": {"zone": "America/Chicago", "quiet": [22, 8], "max_attempts": 3, "retry_window": 300,
                     "initiative": {"advice": {"per_day": 3, "interval": 3600}}},
        "limits": {"queue": 100, "workers": {"default": 2, "test": 2}},
        "bridge": {"accounts": {"butler": {"persona": "butler"}, "tetyana-dyck": {"persona": "tetyana-dyck"}}},
        "routing": {"system1": {"mode": "shadow"}},
        "approval": {"ttl": 900},
    }
    content.update(overrides)
    return content


class Clock:
    def __init__(self, start=1_790_000_000.0):
        self.now = start
        self.lock = threading.Lock()

    def __call__(self):
        with self.lock:
            return self.now

    def advance(self, seconds):
        with self.lock:
            self.now += seconds


class FakeTransport:
    """Records sends; ``script`` controls outcomes: ok, not_dispatched, ambiguous."""

    def __init__(self, deduplicates=False):
        self.sent, self.script, self.deduplicates = [], [], deduplicates
        self.gateway = "gw-1"

    def instance(self):
        return self.gateway

    def send(self, message):
        behaviour = self.script.pop(0) if self.script else "ok"
        if behaviour == "not_dispatched":
            raise NotDispatched("gateway not running")
        if behaviour in ("ambiguous", "ambiguous_restart"):
            self.sent.append(message)
            if behaviour == "ambiguous_restart":
                self.gateway = "gw-2"
            raise Ambiguous("websocket closed")
        self.sent.append(message)
        return f"msg-{len(self.sent)}"


class FileAdapter(Adapter):
    """An "external system" kept in a file, so its effects survive a killed process.

    ``hang`` = "after": record the effect, print EFFECT and hang (killed after the effect);
    ``hang`` = "before": print DISPATCHED and hang without an effect.
    """

    name = "test"

    def __init__(self, path, hang=None):
        self.path, self.hang = Path(path), hang

    def effects(self):
        return self.path.read_text().split() if self.path.exists() else []

    def execute(self, operation, cancel):
        import os
        import time
        key = operation["idempotency_key"]
        if self.hang == "before":
            print("DISPATCHED", flush=True)
            time.sleep(600)
        with open(self.path, "a") as stream:
            stream.write(key + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if self.hang == "after":
            print("EFFECT", flush=True)
            time.sleep(600)
        return {"receipt": f"file:{key}"}

    def reconcile(self, operation):
        found = operation["idempotency_key"] in self.effects()
        return (DONE, {"receipt": f"file:{operation['idempotency_key']}"}) if found else (NOT_DONE, {})


class CoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="butler-core-test-"))
        self.clock = Clock()
        self.adapter = ScriptedAdapter("test")
        self.transport = FakeTransport()
        self.core = self.make_core()
        self.activate(base_config())

    def tearDown(self):
        self.core.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def make_core(self, **kwargs):
        return Core(self.tmp / "state", adapters={"test": kwargs.pop("adapter", self.adapter)},
                    transports={"telegram:butler": self.transport, "telegram:tetyana-dyck": self.transport},
                    clock=self.clock, reserve=0, **kwargs)

    def activate(self, content, core=None):
        core = core or self.core
        version = core.config.propose(content, "test")["version"]
        core.config.activate(version, "test")
        return version

    def request(self, kind="test.write", key="k1", persona="butler", principal="agent:butler", **params):
        return {"kind": kind, "persona": persona, "principal": principal, "target": "test:target",
                "params": params or {"value": 1}, "idempotency_key": key}
