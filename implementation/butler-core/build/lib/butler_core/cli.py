"""``butler-core`` command line: daemon, status, operations, approvals, configuration, backup, diagnostics."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__
from .client import STATE, Client, CoreError, CoreUnavailable, switched


def output(value, as_json):
    if as_json or not isinstance(value, (dict, list)):
        print(json.dumps(value, ensure_ascii=False, indent=2) if not isinstance(value, str) else value)
        return
    print(json.dumps(value, ensure_ascii=False, indent=2))


def parser():
    root = argparse.ArgumentParser(prog="butler-core", description="Butler modular core")
    root.add_argument("--socket", help="core socket path")
    root.add_argument("--json", action="store_true", help="JSON output")
    sub = root.add_subparsers(dest="command", required=True)

    daemon = sub.add_parser("daemon", help="run the core daemon")
    daemon.add_argument("--state", default=str(STATE))

    sub.add_parser("doctor", help="versions, health and alarms")
    sub.add_parser("ping")
    status = sub.add_parser("status", help="task status (all recent tasks or one)")
    status.add_argument("task", nargs="?")
    status.add_argument("--persona")
    status.add_argument("--limit", type=int, default=20)

    for name in ("submit", "execute"):
        cmd = sub.add_parser(name, help=f"{name} an operation through the common path")
        cmd.add_argument("--kind", required=True)
        cmd.add_argument("--persona", required=True)
        cmd.add_argument("--target", required=True)
        cmd.add_argument("--params", default="{}", help="JSON object")
        cmd.add_argument("--key", required=True, help="stable idempotency key")
        cmd.add_argument("--principal")
        cmd.add_argument("--task")
        cmd.add_argument("--timeout", type=float, default=60)

    op = sub.add_parser("operation", help="show one operation")
    op.add_argument("operation")
    ops = sub.add_parser("operations", help="list operations")
    ops.add_argument("--state", action="append")
    ops.add_argument("--limit", type=int, default=30)

    approve = sub.add_parser("approve", help="approve an operation (owner terminal only)")
    approve.add_argument("operation")
    approve.add_argument("code")
    for name in ("reject", "revoke", "cancel"):
        cmd = sub.add_parser(name)
        cmd.add_argument("operation")
    resolve = sub.add_parser("resolve", help="record the externally verified outcome (owner terminal only)")
    resolve.add_argument("operation")
    resolve.add_argument("outcome", choices=["done", "not_done"])
    resolve.add_argument("--evidence", required=True)

    deliver = sub.add_parser("deliver", help="queue a message through core delivery")
    for name in ("persona", "recipient", "channel", "kind", "key"):
        deliver.add_argument(f"--{name}", required=True)
    deliver.add_argument("--text", required=True)
    deliver.add_argument("--task")
    notify = sub.add_parser("notify", help="notice to the owner through core delivery (exit 3: core unavailable)")
    notify.add_argument("--text", required=True)
    notify.add_argument("--key", required=True, help="idempotency key; the same key never sends twice")
    notify.add_argument("--kind", default="status")
    notify.add_argument("--persona", default="butler")
    notify.add_argument("--file", help="send this file as a document with --text as its caption")
    notify.add_argument("--name", help="file name shown to the recipient")
    notify.add_argument("--mime")
    notify.add_argument("--operation")
    notify.add_argument("--wait", type=float, help="seconds to wait for a restarting daemon (default 30)")
    switched = sub.add_parser("switched", help="exit 0 when the scenario runs through the core (reads the switch file)")
    switched.add_argument("scenario")
    switched.add_argument("--state", default=str(STATE))
    lessons = sub.add_parser("lessons", help="System 3 lessons: list | get | confirm | retire | delete | correct | answer | analyse")
    lessons.add_argument("action", choices=["list", "get", "confirm", "retire", "delete", "correct", "answer", "analyse"])
    lessons.add_argument("lesson", nargs="?")
    lessons.add_argument("--text", help="corrected rule (correct) or the owner's answer (answer)")
    lessons.add_argument("--status", help="filter for list")
    deliveries = sub.add_parser("deliveries", help="recent deliveries")
    deliveries.add_argument("--state", action="append")
    deliveries.add_argument("--limit", type=int, default=30)

    config = sub.add_parser("config", help="configuration versions")
    config_sub = config.add_subparsers(dest="action", required=True)
    propose = config_sub.add_parser("propose")
    propose.add_argument("file")
    for name in ("preview", "activate"):
        cmd = config_sub.add_parser(name)
        cmd.add_argument("version", type=int)
    config_sub.add_parser("rollback")
    config_sub.add_parser("versions")
    config_sub.add_parser("active")

    backup = sub.add_parser("backup", help="Storage0 backups")
    backup.add_argument("action", choices=["run", "status"])

    trace = sub.add_parser("trace", help="correlated diagnostics for one request")
    trace.add_argument("correlation")
    metrics = sub.add_parser("metrics", help="latency summaries")
    metrics.add_argument("names", nargs="*")
    sub.add_parser("cron", help="reconcile cron run accounting now")

    admin = sub.add_parser("admin", help="deployment: activate configuration while the daemon is stopped")
    admin.add_argument("action", choices=["activate", "rollback", "versions", "import-deliveries", "restore"])
    admin.add_argument("file", nargs="?", help="configuration JSON (activate), import entries JSON (import-deliveries) "
                                               "or an empty target directory (restore)")
    admin.add_argument("--state", default=str(STATE))
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    if args.command == "daemon":
        from .daemon import run
        return run(Path(args.state))
    if args.command == "admin":
        return admin(args)
    if args.command == "switched":
        return 0 if switched(args.scenario, Path(args.state) / "switches.json") else 1
    client = Client(args.socket)
    try:
        result = dispatch(client, args)
    except CoreUnavailable as error:
        # notify already waited for a restarting daemon (DEC-061); exit 3 tells the caller nothing was sent.
        print(f"butler-core unavailable, retry: {error}", file=sys.stderr)
        return 3 if args.command == "notify" else 2
    except CoreError as error:
        print(f"butler-core: {error.code}: {error.message}", file=sys.stderr)
        return 1
    output(result, args.json)
    return 0


def admin(args):
    """Offline configuration change (DEC-060): refused while a daemon serves the state directory."""
    state = Path(args.state)
    if Client(state / "core.sock").available():
        print("butler-core: the daemon is running; stop it first or use `config activate` from a terminal",
              file=sys.stderr)
        return 3
    from .daemon import adapters
    from .service import Core
    core = Core(state, adapters=adapters())
    if args.action == "versions":
        output(core.config.versions(), True)
        return 0
    if args.action == "rollback":
        output(core.config.rollback("deploy:offline"), True)
        return 0
    if not args.file:
        print(f"butler-core: admin {args.action} needs a file", file=sys.stderr)
        return 2
    if args.action == "restore":
        # The disk-loss drill (REQ-025): the newest complete generation from Storage0 into an empty root, timed.
        from .backup import Backup
        output(Backup(core, version=__version__).restore(Path(args.file)), True)
        return 0
    if args.action == "import-deliveries":
        # Read-only accounting of another system's outcomes (REQ-024): nothing here is ever resent.
        output(core.delivery.import_uncertain(json.loads(Path(args.file).read_text())), True)
        return 0
    content = json.loads(Path(args.file).read_text())
    version, active = core.config.active()
    if active == content:
        output({"unchanged": version}, True)
        return 0
    proposed = core.config.propose(content, "deploy:offline")
    changes = {k: sorted(v) for k, v in proposed["diff"].items()}      # paths only, never values
    activated = core.config.activate(proposed["version"], "deploy:offline")
    output({"activated": activated, "changed_paths": changes}, True)
    return 0


def dispatch(client, args):
    command = args.command
    if command in ("doctor", "ping"):
        return client.call(command)
    if command == "status":
        if args.task:
            return client.call("task.get", task=args.task)
        return client.call("task.list", limit=args.limit, persona=args.persona)
    if command in ("submit", "execute"):
        request = {"kind": args.kind, "persona": args.persona, "target": args.target,
                   "params": json.loads(args.params), "idempotency_key": args.key, "principal": args.principal,
                   "task": args.task}
        if command == "submit":
            return client.call("submit", request=request)
        return client.call("execute", _socket_timeout=args.timeout + 15, request=request, timeout=args.timeout)
    if command == "operation":
        return client.call("operation.get", operation=args.operation)
    if command == "operations":
        return client.call("operation.list", states=args.state, limit=args.limit)
    if command == "approve":
        return client.call("approve", operation=args.operation, code=args.code)
    if command in ("reject", "revoke", "cancel"):
        return client.call(command, operation=args.operation)
    if command == "resolve":
        return client.call("resolve", operation=args.operation, outcome=args.outcome, evidence=args.evidence)
    if command == "deliver":
        return client.call("deliver", persona=args.persona, recipient=args.recipient, channel=args.channel,
                           kind=args.kind, content=args.text, idempotency_key=args.key, task=args.task)
    if command == "notify":
        attachment = None
        if args.file:
            attachment = {"path": str(Path(args.file).resolve()), "name": args.name, "mime": args.mime}
        return client.notify(args.text, args.key, kind=args.kind, persona=args.persona, attachment=attachment,
                             operation=args.operation or os.environ.get("BUTLER_CORE_OPERATION"), wait=args.wait)
    if command == "lessons":
        if args.action == "list":
            return client.call("lessons.list", status=args.status)
        if args.action == "get":
            return client.call("lessons.get", lesson=args.lesson)
        if args.action == "analyse":
            return client.call("lessons.analyse", _socket_timeout=900)
        if args.action == "answer":
            return client.call("lessons.answer", lesson=args.lesson, text=args.text or "")
        status = {"confirm": "confirmed", "retire": "retired", "delete": "deleted", "correct": "confirmed"}[args.action]
        return client.call("lessons.set_status", lesson=args.lesson, status=status, text=args.text)
    if command == "deliveries":
        return client.call("delivery.list", states=args.state, limit=args.limit)
    if command == "config":
        if args.action == "propose":
            return client.call("config.propose", content=json.loads(Path(args.file).read_text()), author="cli")
        if args.action in ("preview", "activate"):
            return client.call(f"config.{args.action}", version=args.version)
        return client.call(f"config.{args.action}")
    if command == "backup":
        return client.call(f"backup.{args.action}", _socket_timeout=3600)
    if command == "trace":
        return client.call("trace", correlation=args.correlation)
    if command == "metrics":
        return client.call("metrics", names=args.names or ["receipt.text", "task.start", "recovery.accounting",
                                                           "observation.available", "approval.wait",
                                                           "external.wait", "delivery.send"])
    if command == "cron":
        return client.call("cron.reconcile")
    raise SystemExit(f"unknown command {command}")


if __name__ == "__main__":
    raise SystemExit(main())
