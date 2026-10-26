"""Python client for the core socket. Butler tools use it to submit operations.

Standard library only and free of package imports: deployment copies this file
into the Butler workspace as ``lib/butler_core_client.py``.
"""
from __future__ import annotations

import json
import os
import socket
import time
import uuid
from pathlib import Path

STATE = Path(os.environ.get("BUTLER_CORE_STATE", Path.home() / ".local/share/butler-core"))
SOCKET = Path(os.environ.get("BUTLER_CORE_SOCKET", STATE / "core.sock"))
SWITCHES = STATE / "switches.json"
# DEC-061: a switched CLI waits this long for a restarting daemon, then refuses its effect.
AVAILABILITY_WAIT = 30.0


class CoreUnavailable(ConnectionError):
    """The daemon is not reachable; nothing was submitted."""


def switched(scenario, path=None):
    """Whether ``scenario`` runs through the core in the active ConfigVersion.

    The core writes the switch file on every activation and at start, so the
    answer is known while the daemon is down. No file means the core was never
    activated and every scenario keeps its legacy path."""
    try:
        data = json.loads(Path(path or SWITCHES).read_text())
    except (OSError, ValueError):
        return False
    return bool(isinstance(data, dict) and data.get("scenarios", {}).get(scenario))


class CoreError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(f"{code}: {message}")
        self.code, self.message = code, message


class Client:
    def __init__(self, path=None, timeout=15.0):
        self.path, self.timeout = Path(path or SOCKET), timeout

    def call(self, method, _socket_timeout=None, **params):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(_socket_timeout or self.timeout)
        try:
            try:
                sock.connect(str(self.path))
            except (FileNotFoundError, ConnectionRefusedError) as error:
                raise CoreUnavailable(f"core daemon unreachable: {type(error).__name__}") from None
            request = {"id": uuid.uuid4().hex[:8], "method": method, "params": params}
            sock.sendall((json.dumps(request, ensure_ascii=False) + "\n").encode())
            stream = sock.makefile("rb")
            line = stream.readline()
        finally:
            sock.close()
        if not line:
            raise CoreError("no_response", "connection closed without a response")
        response = json.loads(line)
        if not response.get("ok"):
            error = response.get("error") or {}
            raise CoreError(error.get("code", "error"), error.get("message", ""))
        return response["result"]

    def call_waiting(self, method, wait=None, **params):
        """``call`` that waits up to ``wait`` seconds (default AVAILABILITY_WAIT) for an unreachable daemon.

        Only a failed connect is retried: the request was never sent, so the
        retry cannot duplicate anything."""
        deadline = time.monotonic() + (AVAILABILITY_WAIT if wait is None else wait)
        while True:
            try:
                return self.call(method, **params)
            except CoreUnavailable:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(1.0)

    def execute(self, kind, persona, target, params, idempotency_key, principal=None, timeout=60, task=None,
                wait=None):
        request = {"kind": kind, "persona": persona, "target": target, "params": params,
                   "idempotency_key": idempotency_key, "principal": principal, "task": task}
        return self.call_waiting("execute", wait, _socket_timeout=timeout + 10, request=request, timeout=timeout)

    def notify(self, text, idempotency_key, kind="status", persona="butler", attachment=None, task=None,
               operation=None, wait=None):
        """Queue a notice to the owner through core delivery; waits for a restarting daemon (DEC-061)."""
        return self.call_waiting("notify", wait, text=text, idempotency_key=idempotency_key, kind=kind,
                                 persona=persona, attachment=attachment, task=task, operation=operation)

    def available(self):
        try:
            self.call("ping", _socket_timeout=2)
            return True
        except (CoreUnavailable, CoreError, OSError):
            return False
