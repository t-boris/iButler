"""Composition root of the core: one instance per state directory.

``Core`` wires the modules together and implements the cross-module flows:
message admission with a receipt after persistence, the chat-turn lifecycle,
explicit continuation, approval and resolution notices, recovery and doctor.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
from zoneinfo import ZoneInfo
import platform
import threading
import time
from pathlib import Path

from . import __version__
from .approvals import Approvals, review_code
from .config import Config
from .cron import agent_of
from .delivery import Delivery
from .diagnostics import Diagnostics
from .events import Events
from .feedback import Feedback
from .operations import OperationError, Operations
from .policy import Policy
from .routing import STATE_RU, Rules, Shadow, short
from .store import StorageExhausted, Store, dumps, loads, new_id
from .tasks import TaskError, Tasks
from .workers import Scheduler

BACKBURNER_CYCLE = "backburner.cycle"
CHAT_TURN_LIMIT = 2 * 3600
TURN_MERGE_GRACE = 30
REJECTED_TEXT = "⚠️ Не могу принять сообщение: хранилище Butler заполнено. Оно не сохранено — повторите позже."


class Core:
    def __init__(self, root, *, adapters=None, transports=None, clock=time.time, model=None, reserve=None,
                 interval=0.25):
        self.root = Path(root)
        self.clock = clock
        kwargs = {} if reserve is None else {"reserve": reserve}
        self.store = Store(self.root, clock=clock, **kwargs)
        self.ingest_store = Store(self.root, name="ingest.sqlite3", clock=clock, **kwargs)
        self.diagnostics = Diagnostics(self.store, clock)
        self.adapters = dict(adapters or {})
        self.transports = transports if transports is not None else {}
        self.config = Config(self.store, self.diagnostics, adapters=set(self.adapters), clock=clock,
                             on_activate=lambda *_: self.write_switches())
        self.policy = Policy(self.store, self.config, self.diagnostics, clock)
        self.approvals = Approvals(self.store, self.policy, self.diagnostics, clock)
        self.events = Events(self.store, self.diagnostics, clock)
        self.ingest = Events(self.ingest_store, self.diagnostics, clock)
        self.tasks = Tasks(self.store, self.config, self.diagnostics, clock)
        self.operations = Operations(self.store, self.config, self.policy, self.approvals, self.diagnostics,
                                     self.adapters, clock, on_change=self._operation_changed)
        from .system3 import System3
        self.lessons = System3(self, clock=clock)
        self.delivery = Delivery(self.store, self.config, self.policy, self.diagnostics, self.transports, clock,
                                 on_change=lambda *_: self.wake())
        self.feedback = Feedback(self.store, clock)
        self.rules = Rules(self)
        self.shadow = Shadow(self.store, self.config, model, clock)
        self.scheduler = Scheduler(self, interval=interval, clock=clock)
        self.changed = threading.Condition()
        self.started_at = self.ready_at = None
        self.recovery = None

    # -- lifecycle -----------------------------------------------------------------

    def start(self, workers=True):
        began = time.monotonic()
        self.started_at = self.clock()
        self.write_switches()
        self.recovery = self.recover()
        self.scheduler.every(10, "track", self.track_running)
        self.scheduler.every(15, "watchdog", self.scheduler.watchdog)
        self.scheduler.every(60, "chat-watchdog", self.interrupt_stale_turns)
        self.scheduler.every(10, "merge-pending", self.merge_pending)
        self.scheduler.every(60, "mail", self.schedule_mail_readings)
        self.scheduler.every(60, "backburner", self.schedule_backburner)
        self.scheduler.every(600, "system3", self.lessons.maybe_run)
        if workers:
            self.scheduler.start()
        self.ready_at = self.clock()
        self.diagnostics.observe("recovery.accounting", (time.monotonic() - began) * 1000,
                                 {"dispatching": len(self.recovery["dispatching"])})
        return self.recovery

    def stop(self):
        self.scheduler.stop()

    def write_switches(self):
        """Publish the active scenario switches atomically for CLIs that must know them while the core is down."""
        path = self.root / "switches.json"
        temporary = path.with_name(".switches.json.tmp")
        temporary.write_text(dumps(self.config.switches()) + "\n")
        temporary.chmod(0o600)
        os.replace(temporary, path)

    def wake(self):
        self.scheduler.wake()
        with self.changed:
            self.changed.notify_all()

    def recover(self):
        found = self.operations.recover()
        for op_id in found["dispatching"]:
            self.scheduler.pool("reconcile").submit(op_id, self.operations.reconcile, op_id)
        found["deliveries"] = self.delivery.recover()
        found["tasks"] = self.store.one("SELECT COUNT(*) FROM tasks")[0]
        self.diagnostics.record("core.recovered", {k: len(v) if isinstance(v, (list, dict)) else v
                                                   for k, v in found.items()})
        return found

    def track_running(self):
        for row in self.store.query("SELECT id FROM operations WHERE state='running'"):
            self.operations.track(row["id"])

    # -- operations ------------------------------------------------------------------

    def _operation_changed(self, op_id, state):
        row = self.operations.get(op_id)
        if row["step"]:
            with self.store.transaction(guard=False) as db:
                self.tasks.follow_operation(db, row)
        if state == "awaiting_approval":
            self.request_approval(row)
        elif state == "needs_reconciliation":
            self.request_resolution(row)
        elif state == "failed" and loads(row["result"], {}).get("interrupted"):
            self.notify_interrupted(row)
        if row["kind"] == "mail.scan":
            self._mail_reading_changed(row, state)
        elif row["kind"] == BACKBURNER_CYCLE:
            self._backburner_cycle_changed(row, state)
        self.wake()

    # -- mail readings (feature butler-email-digest-and-approved-replies) ----------------

    def schedule_mail_readings(self):
        """Submit the day's readings at the configured local times; each slot is one operation, once."""
        if not self.scenario_enabled("mail"):
            return
        _, content = self.config.active()
        rule = (content or {}).get("mail", {})
        zone = ZoneInfo(rule.get("zone") or "America/Chicago")
        now = self.clock()
        local = dt.datetime.fromtimestamp(now, zone)
        for index, slot in enumerate(rule.get("times") or []):
            hour, minute = (int(part) for part in slot.split(":"))
            due = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if local < due or (local - due).total_seconds() > 6 * 3600:
                continue                          # not yet, or too late to still call it this slot
            key = f"mail:scan:{local.date().isoformat()}:{slot}"
            if self.operations.by_key(key) is not None:
                continue
            name = "morning" if index == 0 else "evening" if index == 1 else f"slot{index + 1}"
            self.submit({"kind": "mail.scan", "persona": "butler", "principal": "system:schedule", "target": "mail:inbox",
                         "params": {"slot": name, "lookback_hours": rule.get("lookback_hours", 24)},
                         "idempotency_key": key}, "core")

    def scenario_enabled(self, name):
        _, content = self.config.active()
        return bool((content or {}).get("scenarios", {}).get(name, {}).get("enabled"))

    def _mail_reading_changed(self, op, state):
        if state == "succeeded":
            self.diagnostics.clear_alarm("mail:reading")
            result = loads(op["result"], {})
            if result.get("digest"):
                self.notify(op["persona"], result["digest"], "digest", f"mail-digest:{result.get('reading')}",
                            task=op["task"], operation=op["id"], correlation=op["correlation"])
        elif state == "failed":
            self.diagnostics.raise_alarm("mail:reading", f"mail reading failed: {op['reason']}")

    # -- Back Burner nightly research (feature butler-back-burner-researcher) ----------------

    def schedule_backburner(self):
        """Submit the night's research cycle once per local day, from the start time until the last start time.

        The cycle itself decides which folders are due and stops starting runs at the end of the window; the schedule
        set here is passed on to it, so the owner's configuration is the only place that holds it."""
        if not self.scenario_enabled("backburner"):
            return
        _, content = self.config.active()
        rule = (content or {}).get("backburner", {})
        start, end = rule.get("time") or "00:30", rule.get("last_start") or "06:00"
        local = dt.datetime.fromtimestamp(self.clock(), ZoneInfo(rule.get("zone") or "America/Chicago"))
        now = f"{local:%H:%M}"
        if not start <= now < end:
            return                                # not yet tonight, or the window is over: tomorrow
        key = f"backburner:cycle:{local.date().isoformat()}"
        if self.operations.by_key(key) is not None:
            return
        cycle_id = f"bb-{local:%Y%m%d-%H%M%S}-{hashlib.sha256(key.encode()).hexdigest()[:6]}"
        self.submit({"kind": BACKBURNER_CYCLE, "persona": "butler", "principal": "system:schedule",
                     "target": "backburner:all", "idempotency_key": key,
                     "params": {"cycle_id": cycle_id, "window_start": start, "window_end": end,
                                "max_runs": rule.get("max_runs", 5)}}, "core")

    def _backburner_cycle_changed(self, op, state):
        if state == "failed":
            self.diagnostics.raise_alarm("backburner:cycle", f"back burner cycle failed: {op['reason']}")
        elif state == "succeeded":
            self.diagnostics.clear_alarm("backburner:cycle")
            failing = loads(op["result"], {}).get("failed_folders") or []
            if failing:
                self.diagnostics.raise_alarm("backburner:folders", "back burner folders failing 3 nights in a row: "
                                             + ", ".join(failing))
            else:
                self.diagnostics.clear_alarm("backburner:folders")

    def notify_interrupted(self, op):
        """Long-running work that vanished without a result is reported, never restarted automatically."""
        result = loads(op["result"], {})
        job = result.get("dispatch_id") or op["receipt"] or short(op["id"])
        where = Path(op["target"] or "").name or op["target"] or "—"
        self.notify(op["persona"], f"⚠️ Фоновая задача прервана без результата: {op['kind']} · {where} · {job}.\n"
                    "Повторно не запускаю — напиши, если нужно перезапустить.", "status", f"interrupted:{op['id']}",
                    task=op["task"], operation=op["id"], correlation=op["correlation"])

    def submit(self, request, source, correlation=None):
        """Submit through the common path; operations without a task get one, so status is always visible."""
        request = dict(request)
        result = self.operations.submit(request, source, correlation)
        if result.get("id") and not result.get("duplicate") and not request.get("task"):
            with self.store.transaction(guard=False) as db:
                op = db.execute("SELECT * FROM operations WHERE id=?", (result["id"],)).fetchone()
                task_id = self.tasks.create(db, kind="operation", goal=f"{op['kind']} → {op['target']}",
                                            initiator=op["principal"] or source, persona=op["persona"],
                                            correlation=op["correlation"], channel=source)
                step_id = self.tasks.add_step(db, task_id, {"executor": "operation", "action": op["kind"],
                                                            "state": "running"}, 0)
                db.execute("UPDATE operations SET task=?, step=? WHERE id=?", (task_id, step_id, op["id"]))
                op = db.execute("SELECT * FROM operations WHERE id=?", (op["id"],)).fetchone()
                self.tasks.follow_operation(db, op)
            result["task"] = task_id
        if result.get("state") == "awaiting_approval" and not result.get("duplicate"):
            self.request_approval(self.operations.get(result["id"]))
        self.wake()
        return result

    def start_step(self, step):
        """A ready operation step becomes an operation once; its key makes resubmission idempotent."""
        spec = loads(step["input"], {})
        task = self.store.one("SELECT * FROM tasks WHERE id=?", (step["task"],))
        request = {"kind": step["action"], "persona": task["persona"], "principal": spec.get("principal"),
                   "target": spec.get("target"), "params": spec.get("params", {}), "task": task["id"],
                   "step": step["id"], "idempotency_key": f"step:{step['id']}"}
        try:
            result = self.operations.submit(request, spec.get("source", "core"), task["correlation"])
        except OperationError as error:
            with self.store.transaction(guard=False) as db:
                self.tasks.set_step(db, step["id"], "failed", reason=str(error))
            return
        with self.store.transaction(guard=False) as db:
            if result.get("id"):
                op = db.execute("SELECT * FROM operations WHERE id=?", (result["id"],)).fetchone()
                self.tasks.follow_operation(db, op)
            else:
                self.tasks.set_step(db, step["id"], "failed", reason=result.get("reason"))
        if result.get("state") == "awaiting_approval" and not result.get("duplicate"):
            self.request_approval(self.operations.get(result["id"]))

    # -- owner actions (each one is also a feedback signal for System 3) -----------------

    def _persona_of(self, op_id):
        row = self.store.one("SELECT persona, correlation FROM operations WHERE id=?", (op_id,))
        return (row["persona"], row["correlation"]) if row else (None, None)

    def reject(self, op_id, principal, channel):
        result = self.approvals.reject(op_id, principal, channel)
        persona, correlation = self._persona_of(op_id)
        self.feedback.record("approval_rejected", persona=persona, principal=principal, subject=op_id,
                             correlation=correlation, detail={"channel": channel})
        return result

    def cancel(self, op_id, actor):
        result = self.operations.cancel(op_id, actor)
        persona, correlation = self._persona_of(op_id)
        if actor in ("terminal", "ambient", "gateway") or str(actor).startswith(("telegram:", "terminal:", "voice:")):
            self.feedback.record("cancel", persona=persona, principal=actor, subject=op_id, correlation=correlation,
                                 detail={"outcome": result.get("mode") if isinstance(result, dict) else None})
        return result

    def resolve(self, op_id, outcome, evidence, resolver, channel):
        result = self.operations.resolve(op_id, outcome, evidence, resolver, channel)
        persona, correlation = self._persona_of(op_id)
        self.feedback.record("resolved", persona=persona, principal=resolver, subject=op_id, correlation=correlation,
                             detail={"outcome": outcome, "channel": channel})
        return result

    def signal(self, params, caller):
        """Owner signals reported by an interface (ambient pause/Full Pause, mute, explicit correction)."""
        kind = params.get("kind")
        persona = params.get("persona") or "butler"
        principal = {"ambient": "voice:owner", "terminal": "terminal:owner"}.get(caller, f"{caller}:owner")
        feedback_id = self.feedback.record(kind, persona=persona, principal=principal, subject=params.get("subject"),
                                           correlation=params.get("correlation"), detail=params.get("detail"))
        return {"feedback": feedback_id}

    def wait(self, op_id, timeout=30.0):
        """Block until the operation leaves its in-flight states or the timeout passes."""
        deadline = time.monotonic() + timeout
        while True:
            row = self.operations.get(op_id)
            if row["state"] not in ("requested", "dispatching"):
                return row
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return row
            with self.changed:
                self.changed.wait(min(remaining, 0.5))

    # -- owner notices -----------------------------------------------------------------

    def _owner_target(self, persona):
        _, content = self.config.active()
        notify = (content or {}).get("owner", {}).get("notify")
        return notify or None

    def notify(self, persona, text, kind, key, task=None, operation=None, recipient=None, channel=None,
               correlation=None, attachment=None):
        target = self._owner_target(persona)
        recipient = recipient or (target or {}).get("recipient")
        channel = channel or (target or {}).get("channel")
        if not recipient or not channel:
            self.diagnostics.raise_alarm("notify:unconfigured", "owner notification target is not configured")
            return None
        result = self.delivery.enqueue(recipient=recipient, channel=channel, persona=persona, kind=kind,
                                       content=text[:4000], idempotency_key=key, task=task, operation=operation,
                                       correlation=correlation, attachment=attachment)
        self.wake()
        return result

    def request_approval(self, op):
        params = loads(op["params"], {})
        shown = ", ".join(f"{k}={str(v)[:60]}" for k, v in list(params.items())[:6])
        code = review_code(op["params_hash"])
        text = (f"Нужно подтверждение ({op['persona']}): {op['kind']} → {op['target']}\n"
                f"Параметры: {shown or '—'}\n"
                f"Подтвердить: /approve_{short(op['id'])}_{code}\nОтклонить: /reject_{short(op['id'])}")
        self.notify(op["persona"], text, "approval", f"approval:{op['id']}:{code}", task=op["task"],
                    operation=op["id"], correlation=op["correlation"])

    def request_resolution(self, op):
        text = (f"Результат операции неизвестен ({op['persona']}): {op['kind']} → {op['target']}.\n"
                "Повтор заблокирован. Проверьте результат во внешней системе и ответьте:\n"
                f"/resolve_{short(op['id'])}_done <что проверено>\n/resolve_{short(op['id'])}_notdone <что проверено>")
        self.notify(op["persona"], text, "status", f"resolution:{op['id']}", task=op["task"], operation=op["id"],
                    correlation=op["correlation"])

    # -- text interface: admission and chat turns ---------------------------------------

    def bridge_rule(self, account):
        _, content = self.config.active()
        return (content or {}).get("bridge", {}).get("accounts", {}).get(account)

    def bridge_config(self):
        _, content = self.config.active()
        bridge = (content or {}).get("bridge", {})
        return {"accounts": {name: {"receipt": rule.get("receipt", "👌")}
                             for name, rule in bridge.get("accounts", {}).items()},
                "timeout_ms": bridge.get("timeout_ms", 1200), "core": __version__}

    def admit_message(self, params):
        """Durably record one inbound message and its task; the receipt is allowed only after this returns."""
        account = params.get("account")
        rule = self.bridge_rule(account)
        if rule is None:
            return {"admitted": False, "reason": "account not managed"}
        persona = rule["persona"]
        sender, chat, message_id = str(params.get("sender", "")), str(params.get("chat", "")), \
            str(params.get("message_id", ""))
        principal = f"telegram:{sender}"
        if not sender or not chat or not message_id:
            return {"admitted": False, "reason": "message identity incomplete"}
        if not self.policy.principal_allowed(persona, principal):
            return {"admitted": False, "reason": "sender not authorized for persona"}
        text = params.get("text") if isinstance(params.get("text"), str) else ""
        now = self.clock()
        occurred = params.get("date") if isinstance(params.get("date"), (int, float)) else now
        conversation = f"telegram:{account}:{chat}"
        event = {"source": f"telegram:{account}", "kind": "message", "persona": persona,
                 "conversation": conversation, "principal": principal, "occurred_at": float(occurred),
                 "dedup_key": f"{conversation}:{message_id}", "privacy": "raw_local_only",
                 "payload": {"text": text[:16000], "message_id": message_id, "session": params.get("session_key")}}
        command = self.rules.match(text)
        try:
            normalized = self.events.normalize(event)
            with self.store.transaction() as db:
                row, duplicate = self.events.insert(db, normalized)
                if duplicate:
                    task_id = row["task"]
                else:
                    task_id = self.tasks.create(
                        db, kind="command" if command else "chat", goal=text[:2000] or "(сообщение без текста)",
                        initiator=principal, persona=persona, correlation=row["correlation"], origin_event=row["id"],
                        conversation=conversation, channel=f"telegram:{account}",
                        steps=[{"executor": "rules" if command else "agent",
                                "action": "command" if command else "chat-turn",
                                "input": {"session": params.get("session_key"), "chat": chat}}])
                    self.events.link_task(db, row["id"], task_id)
                    if not command:
                        initiative = self.feedback.last_initiative(db, persona, f"telegram:{chat}", now)
                        self.feedback.record("reply_to_initiative" if initiative else "request", persona=persona,
                                             principal=principal, subject=initiative or task_id,
                                             correlation=row["correlation"], detail={"channel": f"telegram:{account}"},
                                             db=db)
        except StorageExhausted:
            return {"admitted": False, "rejected": "storage", "text": REJECTED_TEXT}
        result = {"admitted": True, "duplicate": duplicate, "task": task_id, "persona": persona,
                  "receipt": rule.get("receipt", "👌")}
        if command and not duplicate:
            reply = self.rules.handle(command, persona=persona, principal=principal,
                                      channel=f"telegram:{account}", correlation=row["correlation"])
            with self.store.transaction(guard=False) as db:
                step = db.execute("SELECT id FROM steps WHERE task=?", (task_id,)).fetchone()
                self.tasks.set_step(db, step["id"], "succeeded", result={"route": "rules"})
            result.update(handled=True, text=reply)
        elif not duplicate:
            self.shadow.observe(row["id"], text, "system2")
        return result

    def _chat_step(self, db, task_id):
        return db.execute("SELECT * FROM steps WHERE task=? AND executor='agent' ORDER BY idx DESC LIMIT 1",
                          (task_id,)).fetchone()

    def _session_steps(self, db, session, states, run=None):
        sql = ("SELECT * FROM steps WHERE executor='agent' AND state IN (%s)" % ",".join("?" * len(states)))
        params = list(states)
        if run is not None:
            sql += " AND json_extract(input,'$.run')=?"
            params.append(run)
        else:
            sql += " AND json_extract(input,'$.session')=?"
            params.append(session)
        return db.execute(sql + " ORDER BY ready_at", params).fetchall()

    def turn_started(self, params):
        """An agent run started in a session: it takes over every message admitted there and not yet started."""
        run_id = str(params.get("run_id") or "")[:100]
        if not run_id:
            return {"ok": False, "reason": "run id required"}
        with self.store.transaction(guard=False) as db:
            if params.get("task"):
                step = self._chat_step(db, params["task"])
                steps = [step] if step is not None and step["state"] in ("ready", "pending") else []
            elif params.get("session_key"):
                steps = self._session_steps(db, str(params["session_key"]), ("ready", "pending"))
            else:
                return {"ok": False, "reason": "session or task required"}
            for step in steps:
                data = loads(step["input"], {})
                data["run"] = run_id
                data.pop("grace", None)
                db.execute("UPDATE steps SET input=? WHERE id=?", (dumps(data), step["id"]))
                self.tasks.set_step(db, step["id"], "running")
        return {"ok": True, "attached": len(steps)}

    def turn_ended(self, params):
        """Close the turns of this run. Messages admitted during the run get a short grace period: the
        gateway either starts a follow-up run for them or has folded them into the run that just ended."""
        run_id, outcome = str(params.get("run_id") or "")[:100], params.get("outcome")
        final = "succeeded" if outcome == "end" else "failed"
        reason = None if final == "succeeded" else f"agent turn {str(params.get('error') or 'error')[:80]}"
        now = self.clock()
        with self.store.transaction(guard=False) as db:
            if params.get("task"):
                step = self._chat_step(db, params["task"])
                steps = [step] if step is not None and step["state"] in ("ready", "pending", "running") else []
            else:
                steps = self._session_steps(db, None, ("running",), run=run_id) if run_id else []
            sessions = set()
            for step in steps:
                sessions.add(loads(step["input"], {}).get("session"))
                self.tasks.set_step(db, step["id"], final, reason=reason, result={"route": "system2", "run": run_id})
            if params.get("session_key"):
                sessions.add(str(params["session_key"]))
            for session in filter(None, sessions):
                for step in self._session_steps(db, session, ("ready",)):
                    data = loads(step["input"], {})
                    data.setdefault("grace", now + TURN_MERGE_GRACE)
                    db.execute("UPDATE steps SET input=? WHERE id=?", (dumps(data), step["id"]))
        state = None
        if params.get("task"):
            state = self.tasks.get(params["task"], history=False)["state"]
        return {"ok": True, "closed": len(steps), "state": state}

    def merge_pending(self):
        """Messages folded into a finished run: no run of their own started within the grace period."""
        now = self.clock()
        merged = []
        with self.store.transaction(guard=False) as db:
            for step in db.execute("SELECT id, task FROM steps WHERE executor='agent' AND state='ready' AND"
                                   " json_extract(input,'$.grace')<?", (now,)).fetchall():
                self.tasks.set_step(db, step["id"], "succeeded", result={"route": "system2", "merged": True},
                                    reason="answered within the preceding agent turn")
                merged.append(step["task"])
        return merged

    def reply_observed(self, params):
        account, chat = params.get("account"), str(params.get("chat", ""))
        rule = self.bridge_rule(account)
        if rule is None or not params.get("message_id"):
            return {"ok": False}
        return self.delivery.observe(recipient=f"telegram:{chat}", channel=f"telegram:{account}",
                                     persona=rule["persona"], kind="reply", receipt=str(params["message_id"]),
                                     idempotency_key=f"sent:{account}:{chat}:{params['message_id']}",
                                     task=params.get("task"))

    def bridge_hello(self, params):
        """A new gateway instance means turns admitted under the previous one can no longer finish."""
        instance = str(params.get("instance", ""))[:100]
        previous = self.store.meta("gateway_instance")
        self.store.set_meta("gateway_instance", instance)
        self.store.set_meta("bridge_version", str(params.get("version", ""))[:40])
        self.store.set_meta("bridge_host", str(params.get("host", ""))[:40])
        self.store.set_meta("bridge_seen", self.clock())
        interrupted = []
        if previous and previous != instance:
            started = float(params.get("started_at") or self.clock())
            interrupted = self.interrupt_turns(before=started, reason="gateway restarted during the turn")
        self.diagnostics.clear_alarm("bridge:unreachable")
        return {"ok": True, "interrupted": len(interrupted), "config": self.bridge_config()}

    def bridge_poll(self):
        """The bridge's periodic configuration refresh doubles as its liveness signal for the doctor."""
        self.bridge_seen()
        return self.bridge_config()

    def bridge_seen(self):
        self.store.set_meta("bridge_seen", self.clock())

    def interrupt_turns(self, before=None, older_than=None, reason="turn interrupted"):
        now = self.clock()
        rows = self.store.query(
            "SELECT s.id step, t.id task, t.persona, t.initiator, t.channel, t.goal, t.created_at FROM steps s"
            " JOIN tasks t ON t.id=s.task WHERE s.executor='agent' AND s.state IN ('ready','pending','running')")
        hit = []
        for row in rows:
            if before is not None and row["created_at"] >= before:
                continue
            if older_than is not None and now - row["created_at"] < older_than:
                continue
            with self.store.transaction(guard=False) as db:
                self.tasks.set_step(db, row["step"], "interrupted", reason=reason)
            hit.append(row["task"])
            channel = row["channel"]
            recipient = row["initiator"]
            goal = " ".join(row["goal"].split())[:80]
            self.notify(row["persona"], f"⚠️ Ход прерван ({reason}): «{goal}».\nПовторного выполнения не будет."
                        f" Чтобы продолжить: /continue_{short(row['task'])}", "status", f"interrupted:{row['step']}",
                        task=row["task"], recipient=recipient, channel=channel)
        return hit

    def interrupt_stale_turns(self):
        return self.interrupt_turns(older_than=CHAT_TURN_LIMIT, reason="нет завершения хода за 2 часа")

    # -- cron accounting (read-only; the core never starts a cron run) ------------------

    def account_cron(self, reader, since=None):
        jobs = reader.jobs()
        since = self.clock() - 7 * 86400 if since is None else since
        recorded = 0
        _, content = self.config.active()
        personas = set((content or {}).get("personas", {}))
        for run in reader.running() + reader.finished(since):
            job = jobs.get(run["job"], {})
            persona = agent_of(run.get("session"), job.get("agent")) or "main"
            persona = persona if persona in personas else "main"
            final = {"ok": "succeeded", "running": "running"}.get(run["status"], "failed")
            with self.store.transaction(guard=False) as db:
                existing = db.execute("SELECT * FROM cron_runs WHERE key=?", (run["key"],)).fetchone()
                if existing is not None and (existing["status"] != "running" or final == "running"):
                    continue
                target = existing
                if target is None and final != "running":
                    target = db.execute("SELECT * FROM cron_runs WHERE job=? AND status='running' AND"
                                        " ABS(run_at-?)<10", (run["job"], run["run_at"])).fetchone()
                if target is not None:
                    db.execute("UPDATE cron_runs SET status=?, duration_ms=?, observed_at=? WHERE key=?",
                               (run["status"], run.get("duration_ms"), self.clock(), target["key"]))
                    step = db.execute("SELECT id FROM steps WHERE task=?", (target["task"],)).fetchone()
                    if step is not None:
                        self.tasks.set_step(db, step["id"], final, reason=None if final == "succeeded" else
                                            f"cron run {run['status']}")
                else:
                    task_id = self.tasks.create(db, kind="cron", goal=job.get("name", run["job"]),
                                                initiator="system:cron", persona=persona,
                                                correlation=run["key"], channel="cron",
                                                steps=[{"executor": "cron", "action": "cron-run", "state": final}])
                    if final != "succeeded":
                        db.execute("UPDATE tasks SET reason=? WHERE id=?", (f"cron run {run['status']}", task_id))
                    db.execute("INSERT INTO cron_runs VALUES(?,?,?,?,?,?,?,?)",
                               (run["key"], run["job"], run["run_at"], persona, run["status"],
                                run.get("duration_ms"), self.clock(), task_id))
                recorded += 1
        return {"recorded": recorded}

    # -- task status and continuation -----------------------------------------------

    def describe_task(self, task):
        lines = [f"Задача {short(task['id'])}: {STATE_RU.get(task['state'], task['state'])}",
                 f"Запрос: {' '.join(task['goal'].split())[:200]}"]
        if task.get("reason"):
            lines.append(f"Причина: {task['reason']}")
        for step in task["steps"]:
            line = f"• шаг {step['index'] + 1} {step['action']}: {STATE_RU.get(step['state'], step['state'])}"
            if step["operation"]:
                op = self.operations.get(step["operation"])
                if op["state"] == "awaiting_approval":
                    line += f" — /approve_{short(op['id'])}_{review_code(op['params_hash'])}"
                elif op["state"] == "needs_reconciliation":
                    line += f" — /resolve_{short(op['id'])}_done|notdone <проверка>"
                elif op["reason"] and op["state"] in ("failed",):
                    line += f" — {op['reason']}"
            lines.append(line)
        return "\n".join(lines)

    def continue_task(self, task_id, *, persona, principal, channel, correlation):
        """Explicit selection; reuses the existing task and never starts a second execution of finished work."""
        task = self.tasks.get(task_id, history=False)
        now = self.clock()
        with self.store.transaction(guard=False) as db:
            db.execute("UPDATE selections SET ended_at=? WHERE principal=? AND channel=? AND ended_at IS NULL",
                       (now, principal, channel))
            db.execute("INSERT INTO selections VALUES(?,?,?,?,?,?,NULL)",
                       (new_id("sl"), principal, channel, "task", task_id, now))
        resumable = task["kind"] == "chat" and task["state"] == "interrupted"
        if resumable and "openclaw.agent_turn" in ((self.config.active()[1] or {}).get("operations", {})):
            origin = self.store.one("SELECT payload FROM events WHERE task=? ORDER BY received_at LIMIT 1",
                                    (task_id,))
            text = loads(origin["payload"], {}).get("text", "") if origin else ""
            with self.store.transaction(guard=False) as db:
                attempt = db.execute("SELECT COUNT(*) FROM steps WHERE task=? AND executor IN ('agent','operation')",
                                     (task_id,)).fetchone()[0]
                session = None
                for step in db.execute("SELECT id, input FROM steps WHERE task=? AND state='interrupted'",
                                       (task_id,)).fetchall():
                    session = loads(step["input"], {}).get("session") or session
                    db.execute("UPDATE steps SET state='superseded' WHERE id=?", (step["id"],))
                self.tasks.add_step(db, task_id, {"executor": "operation", "action": "openclaw.agent_turn",
                                                  "input": {"principal": principal, "source": "rules",
                                                            "target": channel,
                                                            "params": {"task": task_id, "attempt": attempt,
                                                                       "message": text[:8000], "session": session,
                                                                       "chat": task["conversation"].rsplit(":", 1)[-1]}}})
            self.feedback.record("continued", persona=persona, principal=principal, subject=task_id,
                                 correlation=correlation, detail={"channel": channel})
            self.wake()
            return f"Продолжаю задачу {short(task_id)} в этом чате: запрос передан заново один раз."
        return self.describe_task(task) + "\n(Выбрана для продолжения в этом канале.)"

    def selection(self, principal, channel):
        row = self.store.one("SELECT target FROM selections WHERE principal=? AND channel=? AND ended_at IS NULL"
                             " ORDER BY at DESC LIMIT 1", (principal, channel))
        return row[0] if row else None

    # -- diagnostics ------------------------------------------------------------------

    def doctor(self):
        counts = {r[0]: r[1] for r in self.store.query("SELECT state, COUNT(*) FROM tasks GROUP BY state")}
        ops = {r[0]: r[1] for r in self.store.query("SELECT state, COUNT(*) FROM operations GROUP BY state")}
        deliveries = {r[0]: r[1] for r in self.store.query("SELECT state, COUNT(*) FROM deliveries GROUP BY state")}
        version, content = self.config.active()
        last_backup = self.store.meta("backup_last")
        seen = self.store.meta("bridge_seen")
        now = self.clock()
        return {"core": __version__, "python": platform.python_version(), "schema": self.store.schema_version,
                "config": version, "started_at": self.started_at, "ready_at": self.ready_at,
                "tasks": counts, "operations": ops, "deliveries": deliveries,
                "bridge": {"version": self.store.meta("bridge_version"), "host": self.store.meta("bridge_host"),
                           "seen_age_s": None if seen is None else round(now - float(seen))},
                "backup": {"last": None if last_backup is None else float(last_backup),
                           "age_h": None if last_backup is None else round((now - float(last_backup)) / 3600, 2)},
                "free_bytes": self.store.free_bytes(), "alarms": self.diagnostics.alarms(),
                "system1": {"mode": self.shadow.mode(), "model": getattr(self.shadow.model, "health", lambda: None)(),
                            "shadow": self.shadow.report()[:6]},
                "system3": self.lessons.snapshot(),
                "adapters": sorted(self.adapters),
                "scenarios": {k: v.get("enabled") for k, v in (content or {}).get("scenarios", {}).items()}}

    def status(self, task_id=None, limit=20, persona=None):
        if task_id:
            return self.tasks.get(self.tasks.resolve_id(task_id))
        return self.tasks.list(persona=persona, limit=limit)


__all__ = ["Core", "OperationError", "TaskError"]
