"""Owner feedback events for System 3 (REQ-027, DEC-058).

Each owner signal (a pause, a rejection, a cancellation, a correction, a
request, a reply to an initiative) is recorded with references to the Butler
behaviour that preceded it: ids and kinds of recent deliveries and
operations of the same persona. Content is never copied here; the slow
analysis resolves references locally (DEC-059).
"""
from __future__ import annotations

import time

from .store import dumps, loads, new_id

KINDS = ("pause", "full_pause", "resume", "mute", "unmute", "cancel", "approval_rejected", "resolved",
         "correction", "complaint", "request", "reply_to_initiative", "ignored_initiative", "continued",
         "consent_revoked", "silence_request", "not_addressed")
INITIATIVE = ("advice", "daily", "initiative", "digest")
WINDOW = 1800
DETAIL_KEYS = ("source", "scope", "modality", "outcome", "channel", "reason_code", "duration_s", "by", "participant")


class FeedbackError(ValueError):
    pass


def clean_detail(detail):
    """Metadata only: known keys, scalar values, short strings."""
    out = {}
    for key, value in (detail or {}).items():
        if key not in DETAIL_KEYS:
            continue
        if isinstance(value, bool) or isinstance(value, (int, float)):
            out[key] = value
        elif isinstance(value, str):
            out[key] = value[:40]
    return out


class Feedback:
    def __init__(self, store, clock=time.time, window=WINDOW):
        self.store, self.clock, self.window = store, clock, window

    def context(self, db, persona, now):
        since = now - self.window
        deliveries = db.execute("SELECT id, kind, state, created_at FROM deliveries WHERE persona=? AND created_at>=?"
                                " ORDER BY created_at DESC LIMIT 10", (persona, since)).fetchall()
        operations = db.execute("SELECT id, kind, state, created_at FROM operations WHERE persona=? AND"
                                " created_at>=? ORDER BY created_at DESC LIMIT 10", (persona, since)).fetchall()
        return {"deliveries": [{"id": r["id"], "kind": r["kind"], "state": r["state"],
                                "age_s": round(now - r["created_at"])} for r in deliveries],
                "operations": [{"id": r["id"], "kind": r["kind"], "state": r["state"],
                                "age_s": round(now - r["created_at"])} for r in operations]}

    def last_initiative(self, db, persona, recipient, now):
        row = db.execute("SELECT id, kind, created_at FROM deliveries WHERE persona=? AND recipient=? AND state IN"
                         " ('delivered','uncertain') ORDER BY created_at DESC LIMIT 1", (persona, recipient)).fetchone()
        if row is not None and row["kind"] in INITIATIVE and now - row["created_at"] <= self.window:
            return row["id"]
        return None

    def record(self, kind, *, persona, principal, subject=None, correlation=None, detail=None, db=None):
        if kind not in KINDS:
            raise FeedbackError(f"unknown feedback kind {kind!r}")
        now = self.clock()

        def write(connection):
            feedback_id = new_id("fb")
            connection.execute("INSERT INTO feedback VALUES(?,?,?,?,?,?,?,?,?,NULL)",
                               (feedback_id, now, persona, str(principal)[:80], kind,
                                None if subject is None else str(subject)[:80], correlation,
                                dumps(self.context(connection, persona, now)), dumps(clean_detail(detail))))
            return feedback_id

        if db is not None:
            return write(db)
        with self.store.transaction(guard=False) as connection:
            return write(connection)

    def list(self, since=None, kinds=None, persona=None, limit=200, unanalysed=False):
        sql, params, clauses = "SELECT * FROM feedback", [], []
        if since is not None:
            clauses.append("at>=?")
            params.append(since)
        if kinds:
            clauses.append("kind IN (%s)" % ",".join("?" * len(kinds)))
            params.extend(kinds)
        if persona:
            clauses.append("persona=?")
            params.append(persona)
        if unanalysed:
            clauses.append("analysed_at IS NULL")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY at DESC LIMIT ?"
        params.append(limit)
        return [{**{k: row[k] for k in row.keys() if k not in ("context", "detail")},
                 "context": loads(row["context"], {}), "detail": loads(row["detail"], {})}
                for row in self.store.query(sql, params)]

    def summary(self, since=None):
        rows = self.store.query("SELECT kind, persona, COUNT(*) n FROM feedback WHERE at>=? GROUP BY kind, persona",
                                (since or 0,))
        return [dict(r) for r in rows]
