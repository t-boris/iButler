"""The common operation path (REQ-021, REQ-022, REQ-023).

Rules, System 1 and System 2 — and every CLI or interface acting for them —
submit external effects here. Each operation is authorized, optionally
approved, durably recorded before dispatch and executed through its adapter.
A lost response is reconciled; an unknown outcome blocks repetition until the
owner resolves it, and a resolution never dispatches anything.
"""
from __future__ import annotations

import threading
import time

from . import contracts
from .adapters.base import DONE, FAILED, LOST, NOT_DONE, RUNNING, SUCCEEDED, NotDispatched, Rejected
from .approvals import ApprovalError, params_hash, review_code
from .store import dumps, loads, new_id

SOURCES = ("rules", "system1", "system2", "cli", "bridge", "ambient", "cron", "core")
TERMINAL = ("succeeded", "failed", "cancelled")
BACKOFF = (2, 10, 30, 60, 120)


class OperationError(ValueError):
    pass


class QueueFull(OperationError):
    pass


def _contract(row):
    return contracts.validate({"contract": "operation", "version": 1, "id": row["id"], "kind": row["kind"],
                               "persona": row["persona"], "principal": row["principal"], "task": row["task"],
                               "step": row["step"], "params": loads(row["params"]), "target": row["target"],
                               "idempotency_key": row["idempotency_key"], "export": loads(row["export"]),
                               "receipt": row["receipt"], "state": row["state"], "attempts": row["attempts"],
                               "max_attempts": row["max_attempts"]}, "operation")


def summary(row, code=False):
    out = {"id": row["id"], "kind": row["kind"], "persona": row["persona"], "state": row["state"],
           "attempts": row["attempts"], "reason": row["reason"], "receipt": row["receipt"], "task": row["task"],
           "created_at": row["created_at"], "updated_at": row["updated_at"]}
    if row["result"]:
        out["result"] = loads(row["result"])
    if code and row["state"] == "awaiting_approval":
        out["review_code"] = review_code(row["params_hash"])
    return out


class Operations:
    def __init__(self, store, config, policy, approvals, diagnostics, adapters, clock=time.time, on_change=None):
        self.store, self.config, self.policy, self.approvals = store, config, policy, approvals
        self.diagnostics, self.adapters, self.clock = diagnostics, adapters, clock
        self.on_change = on_change or (lambda op_id, state: None)
        self.cancels = {}
        self.lock = threading.Lock()

    def rule(self, kind):
        _, content = self.config.active()
        return (content or {}).get("operations", {}).get(kind)

    def _system1_mode(self):
        _, content = self.config.active()
        return (content or {}).get("routing", {}).get("system1", {}).get("mode", "shadow")

    def _queue_limit(self):
        _, content = self.config.active()
        return (content or {}).get("limits", {}).get("queue", 1000)

    # -- submission ------------------------------------------------------------

    def submit(self, request, source, correlation=None):
        """Authorize and durably record one operation; never executes inline."""
        if source not in SOURCES:
            raise OperationError("unknown submission source")
        if source == "system1" and self._system1_mode() != "active":
            raise OperationError("System 1 is in shadow mode and cannot submit operations")
        if not isinstance(request, dict):
            raise OperationError("operation request object required")
        kind, persona = request.get("kind"), request.get("persona")
        params = request.get("params", {})
        now = self.clock()
        op_id = new_id("op")
        correlation = correlation or request.get("correlation") or op_id
        try:
            record = contracts.make("operation", id=op_id, kind=kind, persona=persona,
                                    principal=request.get("principal"), task=request.get("task"),
                                    step=request.get("step"), params=params, target=request.get("target"),
                                    idempotency_key=request.get("idempotency_key"))
        except contracts.ContractError as error:
            self._reject(source, error, request.get("idempotency_key"))
            raise OperationError(f"invalid operation: {error}") from None
        rule = self.rule(kind)
        digest = params_hash(kind, persona, record["target"], params)
        with self.store.transaction() as db:
            existing = db.execute("SELECT * FROM operations WHERE idempotency_key=?",
                                  (record["idempotency_key"],)).fetchone()
            if existing is not None:
                if existing["params_hash"] != digest:
                    raise OperationError("idempotency key reused with different parameters")
                return summary(existing, code=True) | {"duplicate": True}
            active = db.execute("SELECT COUNT(*) FROM operations WHERE state IN"
                                " ('requested','dispatching','awaiting_approval','running')").fetchone()[0]
            if active >= self._queue_limit():
                raise QueueFull("operation queue is full")
            decision = self.policy.check(persona, "execute", f"action:{kind}", principal=record.get("principal"),
                                         operation=op_id, operation_kind=kind, correlation=correlation, db=db)
            if decision["outcome"] != "allow":
                self.diagnostics.record("operation.denied", {"kind": kind, "persona": persona, "source": source,
                                                             "reason": decision["reason"]}, correlation, db=db)
                return {"id": None, "kind": kind, "state": "denied", "reason": decision["reason"]}
            needs_approval = rule.get("approval", "none") == "required"
            exports = []
            for export in rule.get("export", []):
                resource = f"export:{export['category']}@{export['recipient']}"
                verdict = self.policy.check(persona, "export", resource, principal=record.get("principal"),
                                            operation=op_id, operation_kind=kind, correlation=correlation, db=db)
                if verdict["outcome"] == "deny":
                    self.diagnostics.record("operation.denied", {"kind": kind, "export": resource,
                                                                 "reason": verdict["reason"]}, correlation, db=db)
                    return {"id": None, "kind": kind, "state": "denied", "reason": verdict["reason"]}
                needs_approval = needs_approval or verdict["outcome"] == "approval_required"
                exports.append(resource)
            for name in rule.get("sources", []):
                # The search boundary (REQ-023): a persona reads only the sources it is scoped to.
                verdict = self.policy.check(persona, "search", f"source:{name}", principal=record.get("principal"),
                                            operation=op_id, operation_kind=kind, correlation=correlation, db=db)
                if verdict["outcome"] != "allow":
                    self.diagnostics.record("operation.denied", {"kind": kind, "source": name,
                                                                 "reason": verdict["reason"]}, correlation, db=db)
                    return {"id": None, "kind": kind, "state": "denied", "reason": verdict["reason"]}
            state = "awaiting_approval" if needs_approval else "requested"
            db.execute("INSERT INTO operations(id,kind,persona,principal,task,step,params,params_hash,target,"
                       "idempotency_key,export,state,attempts,max_attempts,created_at,updated_at,correlation,source)"
                       " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0,?,?,?,?,?)",
                       (op_id, kind, persona, record.get("principal"), record.get("task"), record.get("step"), dumps(params),
                        digest, record["target"], record["idempotency_key"],
                        dumps({"exports": exports, "approval": needs_approval}), state,
                        rule.get("max_attempts", 3), now, now, correlation, source))
            self.diagnostics.record("operation.submitted", {"kind": kind, "persona": persona, "source": source,
                                                            "state": state}, correlation, db=db)
            row = db.execute("SELECT * FROM operations WHERE id=?", (op_id,)).fetchone()
        self.on_change(op_id, state)
        return summary(row, code=True)

    def _reject(self, source, error, key):
        try:
            with self.store.transaction(guard=False) as db:
                db.execute("INSERT INTO rejections VALUES(?,?,?,?,?,?)",
                           (new_id("rj"), self.clock(), source, error.reason, error.field,
                            key if isinstance(key, str) else None))
        except OSError:
            pass

    # -- execution ---------------------------------------------------------------

    def by_key(self, idempotency_key):
        """The operation recorded under a caller's idempotency key, or None."""
        return self.store.one("SELECT * FROM operations WHERE idempotency_key=?", (idempotency_key,))

    def get(self, op_id):
        row = self.store.one("SELECT * FROM operations WHERE id=?", (op_id,))
        if row is None:
            raise OperationError("operation not found")
        return row

    def _finish(self, db, op_id, state, reason=None, receipt=None, result=None, attempt_outcome=None, detail=None,
                when=None):
        """Change state; with ``when`` only from those current states (compare-and-set). Return True if changed."""
        now = self.clock()
        guard = "" if when is None else " AND state IN (%s)" % ",".join("?" * len(when))
        changed = db.execute("UPDATE operations SET state=?, reason=COALESCE(?,reason), receipt=COALESCE(?,receipt),"
                             " result=COALESCE(?,result), updated_at=?, finished_at=CASE WHEN ? IN"
                             " ('succeeded','failed','cancelled') THEN ? ELSE finished_at END WHERE id=?" + guard,
                             (state, reason, receipt, None if result is None else dumps(result), now, state, now, op_id,
                              *(when or ()))).rowcount
        if not changed:
            return False
        if attempt_outcome:
            db.execute("UPDATE operation_attempts SET finished_at=?, outcome=?, detail=? WHERE operation=? AND"
                       " n=(SELECT MAX(n) FROM operation_attempts WHERE operation=?) AND finished_at IS NULL",
                       (now, attempt_outcome, detail, op_id, op_id))
        return True

    def _begin(self, op_id):
        """Durably mark one attempt as dispatching; return the contract or None."""
        now = self.clock()
        with self.store.transaction(guard=False) as db:
            row = db.execute("SELECT * FROM operations WHERE id=?", (op_id,)).fetchone()
            if row is None or row["state"] != "requested":
                return None
            rule = self.rule(row["kind"])
            correlation = row["correlation"]
            if rule is None:
                self._finish(db, op_id, "failed", "operation kind is no longer configured")
                return None
            decision = self.policy.check(row["persona"], "execute", f"action:{row['kind']}",
                                         principal=row["principal"], operation=op_id, operation_kind=row["kind"],
                                         correlation=correlation, db=db)
            meta = loads(row["export"], {})
            if decision["outcome"] != "allow":
                self._finish(db, op_id, "failed", "authorization revoked: " + decision["reason"])
                return None
            for resource in meta.get("exports", []):
                verdict = self.policy.check(row["persona"], "export", resource, principal=row["principal"],
                                            operation=op_id, operation_kind=row["kind"], correlation=correlation,
                                            db=db)
                if verdict["outcome"] == "deny":
                    self._finish(db, op_id, "failed", "export authorization revoked: " + verdict["reason"])
                    return None
            if meta.get("approval"):
                try:
                    if row["attempts"] == 0:
                        self.approvals.consume(db, row)
                    else:
                        self.approvals.recheck(db, row)
                except ApprovalError as error:
                    self._finish(db, op_id, "failed", f"approval invalid: {error}")
                    self.diagnostics.record("operation.approval_invalid", {"reason": str(error)}, correlation, db=db)
                    return None
            attempt = row["attempts"] + 1
            db.execute("UPDATE operations SET state='dispatching', attempts=?, dispatched_at=?, updated_at=? WHERE id=?",
                       (attempt, now, now, op_id))
            db.execute("INSERT INTO operation_attempts VALUES(?,?,?,NULL,NULL,NULL)", (op_id, attempt, now))
            ready_at = row["updated_at"] if row["attempts"] == 0 else None
            if ready_at is not None:
                self.diagnostics.observe("task.start", (now - ready_at) * 1000, {"kind": row["kind"]}, db=db)
            self.diagnostics.record("operation.dispatching", {"attempt": attempt}, correlation, db=db)
            row = db.execute("SELECT * FROM operations WHERE id=?", (op_id,)).fetchone()
        return _contract(row)

    def run(self, op_id):
        """Execute one attempt. Safe to call for any id; only ``requested`` operations run."""
        contract = self._begin(op_id)
        if contract is None:
            return self.get(op_id)["state"]
        rule = self.rule(contract["kind"])
        adapter = self.adapters[rule["adapter"]]
        cancel = threading.Event()
        with self.lock:
            self.cancels[op_id] = cancel
        started = self.clock()
        try:
            outcome = adapter.execute(contract, cancel)
        except NotDispatched as error:
            return self._retry_or_fail(op_id, "not_dispatched", error.detail)
        except Rejected as error:
            with self.store.transaction(guard=False) as db:
                changed = self._finish(db, op_id, "failed", f"rejected: {error.detail}", attempt_outcome="rejected",
                                       detail=error.detail, when=("dispatching",))
            if changed:
                self.on_change(op_id, "failed")
            return self.get(op_id)["state"]
        except Exception as error:  # lost response, timeout, crash of the call: outcome unknown
            detail = getattr(error, "detail", type(error).__name__)
            with self.store.transaction(guard=False) as db:
                db.execute("UPDATE operation_attempts SET finished_at=?, outcome='ambiguous', detail=? WHERE"
                           " operation=? AND n=?", (self.clock(), detail, op_id, contract["attempts"]))
            return self.reconcile(op_id)
        finally:
            with self.lock:
                self.cancels.pop(op_id, None)
            self.diagnostics.observe("external.wait", (self.clock() - started) * 1000, {"kind": contract["kind"]})
        receipt = str(outcome.get("receipt", ""))[:500] or None
        state = "running" if adapter.tracked else "succeeded"
        with self.store.transaction(guard=False) as db:
            # A receipt proves the effect, even if a cancellation was recorded meanwhile.
            changed = self._finish(db, op_id, state, receipt=receipt, result=outcome.get("result"),
                                   attempt_outcome="ok", when=("dispatching", "cancelled"))
            correlation = db.execute("SELECT correlation FROM operations WHERE id=?", (op_id,)).fetchone()[0]
            if changed:
                self.diagnostics.record("operation." + state, {"kind": contract["kind"]}, correlation, db=db)
        if changed:
            self.on_change(op_id, state)
        return self.get(op_id)["state"]

    def _retry_or_fail(self, op_id, outcome, detail):
        now = self.clock()
        with self.store.transaction(guard=False) as db:
            row = db.execute("SELECT * FROM operations WHERE id=?", (op_id,)).fetchone()
            when = ("dispatching", "needs_reconciliation")
            if row["attempts"] < row["max_attempts"]:
                delay = BACKOFF[min(row["attempts"] - 1, len(BACKOFF) - 1)]
                state = "requested"
                changed = self._finish(db, op_id, state, f"retry after {outcome}", attempt_outcome=outcome,
                                       detail=detail, when=when)
                if changed:
                    db.execute("UPDATE operations SET not_before=? WHERE id=?", (now + delay, op_id))
            else:
                state = "failed"
                changed = self._finish(db, op_id, state, f"{outcome}; retries exhausted", attempt_outcome=outcome,
                                       detail=detail, when=when)
            if changed:
                self.diagnostics.record("operation." + state, {"after": outcome}, row["correlation"], db=db)
        if not changed:
            return self.get(op_id)["state"]
        self.on_change(op_id, state)
        return state

    def ready(self, now=None):
        """Requested operations whose retry backoff has passed."""
        now = self.clock() if now is None else now
        return [r["id"] for r in self.store.query(
            "SELECT id FROM operations WHERE state='requested' AND (not_before IS NULL OR not_before<=?)"
            " ORDER BY created_at", (now,))]

    # -- reconciliation and resolution --------------------------------------------

    def reconcile(self, op_id):
        row = self.get(op_id)
        if row["state"] not in ("dispatching", "needs_reconciliation"):
            return row["state"]
        rule = self.rule(row["kind"]) or {}
        adapter = self.adapters.get(rule.get("adapter"))
        if adapter is None or rule.get("reconcile", "auto") == "owner":
            outcome, evidence = "unknown", {"reason": "automatic reconciliation not available"}
        else:
            try:
                outcome, evidence = adapter.reconcile(_contract(row))
            except Exception as error:
                outcome, evidence = "unknown", {"reason": f"reconciliation failed: {type(error).__name__}"}
        with self.store.transaction(guard=False) as db:
            db.execute("INSERT INTO reconciliations VALUES(?,?,?,?,?)",
                       (new_id("rc"), op_id, self.clock(), outcome, dumps(evidence)))
            self.diagnostics.record("operation.reconciled", {"outcome": outcome}, row["correlation"], db=db)
        if outcome == DONE:
            state = "running" if adapter.tracked else "succeeded"
            with self.store.transaction(guard=False) as db:
                changed = self._finish(db, op_id, state, "reconciled: effect found", receipt=evidence.get("receipt"),
                                       result=evidence.get("result"), when=("dispatching", "needs_reconciliation",
                                                                             "cancelled"))
            if changed:
                self.on_change(op_id, state)
            return self.get(op_id)["state"]
        if outcome == NOT_DONE:
            return self._retry_or_fail(op_id, "reconciled_not_done", "effect not found")
        with self.store.transaction(guard=False) as db:
            changed = self._finish(db, op_id, "needs_reconciliation", "outcome unknown; owner resolution required",
                                   when=("dispatching",))
            if changed:
                self.diagnostics.record("operation.needs_reconciliation", {"kind": row["kind"]}, row["correlation"],
                                        db=db)
        if not changed:
            return self.get(op_id)["state"]
        self.on_change(op_id, "needs_reconciliation")
        return "needs_reconciliation"

    def resolve(self, op_id, outcome, evidence, resolver, channel):
        """Owner records the externally verified outcome. Never dispatches anything."""
        if outcome not in (DONE, NOT_DONE):
            raise OperationError("outcome must be done or not_done")
        if not isinstance(evidence, str) or not evidence.strip():
            raise OperationError("evidence of the external check is required")
        now = self.clock()
        with self.store.transaction(guard=False) as db:
            row = db.execute("SELECT * FROM operations WHERE id=?", (op_id,)).fetchone()
            if row is None or row["state"] != "needs_reconciliation":
                raise OperationError("operation does not need resolution")
            if not self.policy.approver_allowed(row["persona"], resolver):
                raise OperationError("resolver is not authorized for this persona")
            db.execute("INSERT INTO resolutions VALUES(?,?,?,?,?,?,?)",
                       (new_id("rs"), op_id, now, resolver, channel, outcome, evidence.strip()[:2000]))
            state = "succeeded" if outcome == DONE else "failed"
            self._finish(db, op_id, state, f"owner resolved: {outcome}")
            self.diagnostics.record("operation.resolved", {"outcome": outcome, "channel": channel},
                                    row["correlation"], db=db)
        self.on_change(op_id, state)
        return {"operation": op_id, "state": state}

    # -- tracking and cancellation -------------------------------------------------

    def track(self, op_id):
        row = self.get(op_id)
        if row["state"] != "running":
            return row["state"]
        rule = self.rule(row["kind"]) or {}
        adapter = self.adapters.get(rule.get("adapter"))
        if adapter is None:
            return "running"
        status, result = adapter.track(_contract(row))
        if status == RUNNING:
            return "running"
        state = {SUCCEEDED: "succeeded", FAILED: "failed", LOST: "needs_reconciliation"}[status]
        reason = {"failed": "external work failed", "needs_reconciliation": "external work lost; owner check needed"}
        reason = (result or {}).pop("reason", None) or reason.get(state)
        with self.store.transaction(guard=False) as db:
            changed = self._finish(db, op_id, state, reason, result=result or None, when=("running",))
        if changed:
            self.on_change(op_id, state)
        return self.get(op_id)["state"]

    def cancel(self, op_id, actor):
        """Distinguish stopping a wait, stopping an executor and an effect that already happened."""
        row = self.get(op_id)
        state = row["state"]
        if state in ("awaiting_approval", "requested"):
            with self.store.transaction(guard=False) as db:
                current = db.execute("SELECT state FROM operations WHERE id=?", (op_id,)).fetchone()["state"]
                if current in ("awaiting_approval", "requested"):
                    self._finish(db, op_id, "cancelled", f"wait cancelled by {actor}")
                    cancelled = True
                else:
                    cancelled = False
            if cancelled:
                self.on_change(op_id, "cancelled")
                return {"operation": op_id, "mode": "cancel_wait", "state": "cancelled", "effect": "none"}
            row = self.get(op_id)
            state = row["state"]
        if state in ("dispatching", "running"):
            with self.lock:
                event = self.cancels.get(op_id)
            if event is not None:
                event.set()
            rule = self.rule(row["kind"]) or {}
            adapter = self.adapters.get(rule.get("adapter"))
            stopped = adapter.cancel(_contract(row)) if adapter else {"stopped": False, "effect": "unknown"}
            effect = stopped.get("effect", "unknown")
            # "partial": long-running work stopped midway; its partial changes are reported, not reconciled.
            final = {"none": "cancelled", "partial": "cancelled", "done": "succeeded"}.get(effect,
                                                                                         "needs_reconciliation")
            with self.store.transaction(guard=False) as db:
                current = db.execute("SELECT state FROM operations WHERE id=?", (op_id,)).fetchone()["state"]
                if current in ("dispatching", "running"):
                    self._finish(db, op_id, final, f"executor stop requested by {actor}; effect {effect}")
                else:
                    final = current
            self.on_change(op_id, final)
            mode = "effect_done" if final == "succeeded" else "stop_executor"
            return {"operation": op_id, "mode": mode, "state": final, "effect": effect}
        if state == "succeeded":
            return {"operation": op_id, "mode": "effect_done", "state": state, "effect": "done"}
        return {"operation": op_id, "mode": "none", "state": state, "effect": "unknown" if state ==
                "needs_reconciliation" else "none"}

    # -- recovery ---------------------------------------------------------------

    def recover(self):
        """After a restart: lost-response operations reconcile, tracked ones resume, queued ones stay queued."""
        lost = [r["id"] for r in self.store.query("SELECT id FROM operations WHERE state='dispatching'")]
        running = [r["id"] for r in self.store.query("SELECT id FROM operations WHERE state='running'")]
        return {"dispatching": lost, "running": running, "requested": self.ready(float("inf"))}

    def list(self, states=None, limit=50, persona=None):
        sql, params = "SELECT * FROM operations", []
        clauses = []
        if states:
            clauses.append("state IN (%s)" % ",".join("?" * len(states)))
            params.extend(states)
        if persona:
            clauses.append("persona=?")
            params.append(persona)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [summary(r, code=True) for r in self.store.query(sql, params)]
