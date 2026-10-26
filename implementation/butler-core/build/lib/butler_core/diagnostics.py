"""Correlated, redacted diagnostics and latency metrics (REQ-025).

Nothing recorded here may contain a secret or prohibited content: values under
secret-like keys are dropped, free text (messages, prompts, transcripts) is
reduced to its length, and long digit runs (chat ids, phone numbers) are masked.
"""
from __future__ import annotations

import re
import time

from .store import dumps, loads

SECRET_KEY = re.compile(r"(token|secret|password|passwd|api[_-]?key|authorization|cookie|credential|session_?string|private)",
                        re.I)
TEXT_KEY = re.compile(r"^(text|content|body|prompt|message|transcript|caption|goal|query|payload|input|output)$", re.I)
SECRET_VALUE = re.compile(r"(\b\d{6,12}:[A-Za-z0-9_-]{30,}\b|\bsk-[A-Za-z0-9_-]{16,}|\bgh[pousr]_[A-Za-z0-9]{20,}|"
                          r"\bgithub_pat_[A-Za-z0-9_]{20,}|\bAIza[0-9A-Za-z_-]{30,}|\bya29\.[0-9A-Za-z_.-]{20,}|"
                          r"\bxox[abprs]-[A-Za-z0-9-]{10,}|\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,})")
DIGITS = re.compile(r"(?<![\w.])-?\d{7,}(?![\w.])")
LATENCY = ("receipt.text", "receipt.voice", "task.start", "observation.available", "approval.wait",
           "external.wait", "completion", "recovery.accounting", "delivery.send")


def mask_text(text):
    text = SECRET_VALUE.sub("[secret]", text)
    return DIGITS.sub(lambda m: "#" * 4 + m.group(0)[-3:], text)


def redact(value, key=None, depth=0):
    """Return a copy that is safe to store in diagnostics or print."""
    if depth > 6:
        return "[nested]"
    if key is not None and SECRET_KEY.search(str(key)):
        return "[secret]"
    if key is not None and TEXT_KEY.match(str(key)) and isinstance(value, str):
        return f"[text:{len(value)}]"
    if isinstance(value, dict):
        return {str(k): redact(v, k, depth + 1) for k, v in list(value.items())[:50]}
    if isinstance(value, (list, tuple)):
        return [redact(v, None, depth + 1) for v in list(value)[:50]]
    if isinstance(value, str):
        return mask_text(value[:300])
    if isinstance(value, (int, float)) and not isinstance(value, bool) and abs(value) >= 10**9 and float(value).is_integer() \
            and key not in ("at", "occurred_at", "received_at", "created_at", "updated_at", "run_at"):
        return "#" * 4 + str(int(value))[-3:]
    return value


class Diagnostics:
    def __init__(self, store, clock=time.time):
        self.store, self.clock = store, clock

    def record(self, kind, detail=None, correlation=None, db=None):
        row = (self.clock(), correlation, kind, dumps(redact(detail or {})))
        sql = "INSERT INTO diagnostics(at,correlation,kind,detail) VALUES(?,?,?,?)"
        if db is not None:
            db.execute(sql, row)
            return
        try:
            with self.store.transaction(guard=False) as tx:
                tx.execute(sql, row)
        except OSError:
            pass  # diagnostics never block work

    def observe(self, name, value_ms, labels=None, db=None):
        if name not in LATENCY:
            raise ValueError(f"unknown metric {name}")
        row = (self.clock(), name, float(value_ms), dumps(redact(labels or {})))
        sql = "INSERT INTO metrics(at,name,value,labels) VALUES(?,?,?,?)"
        if db is not None:
            db.execute(sql, row)
            return
        try:
            with self.store.transaction(guard=False) as tx:
                tx.execute(sql, row)
        except OSError:
            pass

    def summary(self, name, since=None):
        rows = self.store.query("SELECT value FROM metrics WHERE name=? AND at>=? ORDER BY value",
                                (name, since if since is not None else 0))
        values = [r[0] for r in rows]
        if not values:
            return {"name": name, "count": 0}

        def pct(p):
            return round(values[min(len(values) - 1, int(round(p * (len(values) - 1))))], 1)
        return {"name": name, "count": len(values), "p50": pct(.5), "p95": pct(.95), "max": round(values[-1], 1)}

    def raise_alarm(self, key, detail):
        with self.store.transaction(guard=False) as db:
            db.execute("INSERT INTO alarms VALUES(?,?,?,NULL) ON CONFLICT(key) DO UPDATE SET detail=excluded.detail,"
                       " cleared_at=NULL, raised_at=CASE WHEN alarms.cleared_at IS NULL THEN alarms.raised_at"
                       " ELSE excluded.raised_at END", (key, self.clock(), mask_text(str(detail))[:300]))

    def clear_alarm(self, key):
        with self.store.transaction(guard=False) as db:
            db.execute("UPDATE alarms SET cleared_at=? WHERE key=? AND cleared_at IS NULL", (self.clock(), key))

    def alarms(self):
        return [dict(r) for r in self.store.query("SELECT key,raised_at,detail FROM alarms WHERE cleared_at IS NULL")]

    def trace(self, correlation):
        """Every record carrying one correlation id, across the request lifecycle."""
        result = {"correlation": correlation}
        for table, columns in (("events", "id,source,kind,persona,received_at,task"),
                               ("tasks", "id,kind,persona,state,created_at,updated_at,reason"),
                               ("operations", "id,kind,persona,state,attempts,source,reason,receipt"),
                               ("deliveries", "id,channel,kind,state,attempts,reason"),
                               ("policy_decisions", "boundary,resource,outcome,reason,at")):
            result[table] = [dict(r) for r in self.store.query(
                f"SELECT {columns} FROM {table} WHERE correlation=?", (correlation,))]
        result["diagnostics"] = [{"at": r["at"], "kind": r["kind"], "detail": loads(r["detail"])}
                                 for r in self.store.query("SELECT at,kind,detail FROM diagnostics WHERE correlation=?"
                                                           " ORDER BY id", (correlation,))]
        return redact(result)
