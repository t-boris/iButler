"""The core daemon (``butler-core daemon``): the only writer of the core databases.

It composes the core with the real adapters and transports, serves the local
socket, accounts cron runs, writes backups and reconciles unfinished work on
start. A first start with no active configuration activates the deployment's
bootstrap configuration; every later change goes through ``config activate``.
"""
from __future__ import annotations

import json
import os
import re
import signal
import sys
import threading
import time
from pathlib import Path

from . import __version__
from .adapters.openclaw import AgentTurnAdapter, TelegramTransport
from .backup import Backup
from .config import ConfigError
from .cron import CronReader
from .server import Server
from .service import Core

ACCOUNT = re.compile(r"^telegram:([a-z0-9][a-z0-9-]{0,40})$")


class Transports:
    """Telegram transports created per bot account on first use (channel ``telegram:<account>``)."""

    def __init__(self, factory=TelegramTransport):
        self.factory, self.cache, self.lock = factory, {}, threading.Lock()

    def get(self, channel, default=None):
        match = ACCOUNT.match(str(channel))
        if not match:
            return default
        with self.lock:
            if channel not in self.cache:
                self.cache[channel] = self.factory(match.group(1))
            return self.cache[channel]

    def __contains__(self, channel):
        return ACCOUNT.match(str(channel)) is not None


def adapters():
    """Adapters available to operations; configuration decides which operation kinds use them."""
    found = {"openclaw": AgentTurnAdapter()}
    try:
        from .adapters import butler
    except ImportError:
        return found
    found.update(butler.adapters())
    return found


def shadow_model():
    try:
        from .system1 import load
    except ImportError:
        return None
    return load()


class CronJob:
    def __init__(self, core, reader=None):
        self.core, self.reader = core, reader or CronReader()
        self.lock = threading.Lock()

    def reconcile(self):
        with self.lock:
            return self.core.account_cron(self.reader)


def bootstrap(core, root):
    """Activate the deployment's initial configuration once, when nothing is active yet."""
    version, _ = core.config.active()
    path = Path(root) / "bootstrap-config.json"
    if version or not path.is_file():
        return version
    content = json.loads(path.read_text())
    proposed = core.config.propose(content, "deploy:bootstrap")
    core.config.activate(proposed["version"], "deploy:bootstrap")
    return proposed["version"]


def build(root, *, workers=True):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    core = Core(root, adapters=adapters(), transports=Transports(), model=shadow_model())
    try:
        bootstrap(core, root)
    except (ConfigError, ValueError, OSError) as error:
        core.diagnostics.raise_alarm("config:bootstrap", f"bootstrap configuration rejected: {error}")
    backup = Backup(core, version=__version__)
    cron = CronJob(core)
    server = Server(core, root / "core.sock", backup=backup, cron=cron)
    core.scheduler.every(30, "cron", cron.reconcile, delay=5)
    core.scheduler.every(600, "backup", lambda: core.scheduler.pool("backup").submit("backup", backup.maybe_run),
                         delay=120)
    return core, server


def run(root):
    began = time.monotonic()
    core, server = build(root)
    stopping = threading.Event()

    def stop(*_):
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    server.start()
    recovery = core.start()
    print(json.dumps({"event": "core.started", "version": __version__, "pid": os.getpid(),
                      "recovered": {k: len(v) if isinstance(v, (list, dict)) else v for k, v in recovery.items()},
                      "seconds": round(time.monotonic() - began, 2)}), flush=True)
    while not stopping.wait(1):
        pass
    server.stop()
    core.stop()
    print(json.dumps({"event": "core.stopped", "pid": os.getpid()}), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(run(Path(sys.argv[1])))
