"""Persisted tasks and steps with one consistent lifecycle (REQ-022, REQ-024).

A task is an accepted request; its steps are executed by operations or by an
agent turn. The task state is derived from its steps and every change is kept
in the task history with its reason, so status survives any process failure.
"""
from __future__ import annotations

import time

from . import contracts
from .store import dumps, loads, new_id

OPERATION_TO_STEP = {"awaiting_approval": "awaiting_approval", "requested": "running", "dispatching": "running",
                     "running": "running", "succeeded": "succeeded", "failed": "failed", "cancelled": "cancelled",
                     "needs_reconciliation": "needs_reconciliation"}
TERMINAL = ("succeeded", "failed", "cancelled")


class TaskError(ValueError):
    pass


def derive(states):
    if not states:
        return "accepted"
    present = set(states)
    for state in ("needs_reconciliation", "interrupted", "failed"):
        if state in present:
            return state
    if "awaiting_approval" in present:
        return "awaiting_approval"
    if "running" in present:
        return "running"
    if present <= {"succeeded"}:
        return "succeeded"
    if present <= {"succeeded", "cancelled"}:
        return "cancelled"
    return "scheduled"


class Tasks:
    def __init__(self, store, config, diagnostics, clock=time.time):
        self.store, self.config, self.diagnostics, self.clock = store, config, diagnostics, clock

    def create(self, db, *, kind, goal, initiator, persona, correlation, origin_event=None, conversation=None,
               channel=None, steps=(), deadline=None):
        now = self.clock()
        version, _ = self.config.active()
        task_id = new_id("tk")
        record = contracts.make("task", id=task_id, kind=kind, goal=goal[:2000], initiator=initiator,
                                persona=persona, state="accepted", created_at=now, updated_at=now,
                                deadline=deadline, config_version=version or 1, conversation=conversation,
                                events=[origin_event] if origin_event else [])
        db.execute("INSERT INTO tasks VALUES(?,?,?,?,?,?,?,?,?,?,?,NULL,?,?,?)",
                   (task_id, kind, record["goal"], initiator, persona, "accepted", now, now, deadline,
                    version or 0, conversation, correlation, origin_event, channel))
        db.execute("INSERT INTO task_history VALUES(?,?,?,?)", (task_id, now, "accepted", None))
        for index, step in enumerate(steps):
            self.add_step(db, task_id, step, index)
        self.refresh(db, task_id)
        return task_id

    def add_step(self, db, task_id, step, index=None):
        if index is None:
            index = (db.execute("SELECT MAX(idx) FROM steps WHERE task=?", (task_id,)).fetchone()[0] or 0) + 1
        step_id = new_id("st")
        depends = step.get("depends_on", [])
        state = step.get("state") or ("pending" if depends else "ready")
        record = contracts.make("task_step", id=step_id, task=task_id, index=index, executor=step["executor"],
                                action=step["action"], depends_on=depends, max_attempts=step.get("max_attempts", 1),
                                state=state, input=step.get("input", {}))
        now = self.clock()
        db.execute("INSERT INTO steps VALUES(?,?,?,?,?,?,0,?,?,?,NULL,NULL,NULL,?,NULL,NULL,?)",
                   (step_id, task_id, index, record["executor"], record["action"], dumps(depends),
                    record["max_attempts"], state, dumps(record["input"]), now if state == "ready" else None,
                    float(step.get("timeout", 7200))))
        return step_id

    def set_step(self, db, step_id, state, *, result=None, reason=None, operation=None):
        now = self.clock()
        row = db.execute("SELECT * FROM steps WHERE id=?", (step_id,)).fetchone()
        if row is None:
            raise TaskError("step not found")
        db.execute("UPDATE steps SET state=?, result=COALESCE(?,result), operation=COALESCE(?,operation),"
                   " started_at=CASE WHEN ?='running' AND started_at IS NULL THEN ? ELSE started_at END,"
                   " finished_at=CASE WHEN ? IN ('succeeded','failed','cancelled','interrupted',"
                   "'needs_reconciliation') THEN ? ELSE finished_at END,"
                   " attempts=attempts+CASE WHEN ?='running' AND state!='running' THEN 1 ELSE 0 END WHERE id=?",
                   (state, None if result is None else dumps(result), operation, state, now, state, now, state,
                    step_id))
        self._unblock(db, row["task"])
        return self.refresh(db, row["task"], reason)

    def _unblock(self, db, task_id):
        steps = db.execute("SELECT id,idx,state,depends_on FROM steps WHERE task=?", (task_id,)).fetchall()
        done = {s["idx"] for s in steps if s["state"] == "succeeded"}
        for step in steps:
            if step["state"] == "pending" and set(loads(step["depends_on"], [])) <= done:
                db.execute("UPDATE steps SET state='ready', ready_at=? WHERE id=?", (self.clock(), step["id"]))

    def refresh(self, db, task_id, reason=None):
        row = db.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
        states = [s[0] for s in db.execute("SELECT state FROM steps WHERE task=? AND state!='superseded'",
                                           (task_id,)).fetchall()]
        state = derive([{"ready": "scheduled", "pending": "scheduled"}.get(s, s) for s in states])
        if row["state"] != state:
            now = self.clock()
            db.execute("UPDATE tasks SET state=?, updated_at=?, reason=COALESCE(?,reason) WHERE id=?",
                       (state, now, reason, task_id))
            db.execute("INSERT INTO task_history VALUES(?,?,?,?)", (task_id, now, state, reason))
        return state

    def follow_operation(self, db, op_row):
        """Mirror an operation's state onto the step that owns it."""
        if not op_row["step"]:
            return None
        state = OPERATION_TO_STEP[op_row["state"]]
        result = {"operation": op_row["id"], "receipt": op_row["receipt"]} if state in TERMINAL else None
        return self.set_step(db, op_row["step"], state, result=result, reason=op_row["reason"],
                             operation=op_row["id"])

    def resolve_id(self, prefix):
        """Full task id from a full id or a unique short prefix (as shown in chat commands)."""
        if not isinstance(prefix, str) or len(prefix) < 6:
            raise TaskError("task id required")
        full = prefix if prefix.startswith("tk-") else "tk-" + prefix
        rows = self.store.query("SELECT id FROM tasks WHERE id LIKE ? ESCAPE '\\' LIMIT 2",
                                (full.replace("_", "\\_") + "%",))
        if len(rows) != 1:
            raise TaskError("task not found" if not rows else "task id is ambiguous")
        return rows[0][0]

    def get(self, task_id, history=True):
        row = self.store.one("SELECT * FROM tasks WHERE id=?", (task_id,))
        if row is None:
            raise TaskError("task not found")
        out = {k: row[k] for k in ("id", "kind", "goal", "initiator", "persona", "state", "created_at", "updated_at",
                                   "deadline", "reason", "conversation", "channel", "correlation")}
        out["steps"] = [{"id": s["id"], "index": s["idx"], "executor": s["executor"], "action": s["action"],
                         "state": s["state"], "attempts": s["attempts"], "operation": s["operation"],
                         "result": loads(s["result"])}
                        for s in self.store.query("SELECT * FROM steps WHERE task=? ORDER BY idx", (task_id,))]
        if history:
            out["history"] = [dict(h) for h in self.store.query(
                "SELECT at,state,reason FROM task_history WHERE task=? ORDER BY rowid", (task_id,))]
        return out

    def list(self, states=None, persona=None, limit=20, kinds=None):
        sql, params, clauses = "SELECT id FROM tasks", [], []
        if kinds:
            clauses.append("kind IN (%s)" % ",".join("?" * len(kinds)))
            params.extend(kinds)
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
        return [self.get(r[0], history=False) for r in self.store.query(sql, params)]

    def ready_steps(self, executor=None):
        sql = "SELECT * FROM steps WHERE state='ready'"
        params = ()
        if executor:
            sql += " AND executor=?"
            params = (executor,)
        return self.store.query(sql + " ORDER BY ready_at", params)
