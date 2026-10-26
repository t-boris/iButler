"""Durable event admission with stable deduplication (REQ-022).

``admit`` returns only after the event is committed to disk; repeated input
with the same stable key returns the original event instead of new work.
"""
from __future__ import annotations

import time

from . import contracts
from .store import StorageExhausted, dumps, loads, new_id


class Events:
    def __init__(self, store, diagnostics, clock=time.time):
        self.store, self.diagnostics, self.clock = store, diagnostics, clock

    def record_rejection(self, source, error, dedup_key=None):
        try:
            with self.store.transaction(guard=False) as db:
                db.execute("INSERT INTO rejections VALUES(?,?,?,?,?,?)",
                           (new_id("rj"), self.clock(), str(source)[:100], getattr(error, "reason", str(error))[:300],
                            getattr(error, "field", None), dedup_key if isinstance(dedup_key, str) else None))
        except OSError:
            pass

    def normalize(self, data):
        data = dict(data)
        data.setdefault("contract", "event")
        data.setdefault("version", contracts.SUPPORTED["event"][-1])
        data.setdefault("id", new_id("ev"))
        data.setdefault("received_at", self.clock())
        try:
            return contracts.validate(data, "event")
        except contracts.ContractError as error:
            self.record_rejection(data.get("source", "unknown"), error, data.get("dedup_key"))
            raise

    def insert(self, db, event):
        """Insert inside the caller's transaction; return (row, duplicate)."""
        existing = db.execute("SELECT * FROM events WHERE dedup_key=?", (event["dedup_key"],)).fetchone()
        if existing is not None:
            return existing, True
        db.execute("INSERT INTO events VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL)",
                   (event["id"], event["dedup_key"], event["source"], event["kind"], event["persona"],
                    event.get("conversation"), event.get("principal"), event["privacy"], event["occurred_at"],
                    event["received_at"], dumps(event.get("payload", {})), dumps(event.get("refs", [])),
                    event.get("correlation") or event["id"]))
        return db.execute("SELECT * FROM events WHERE id=?", (event["id"],)).fetchone(), False

    def admit(self, data):
        event = self.normalize(data)
        try:
            with self.store.transaction() as db:
                row, duplicate = self.insert(db, event)
        except StorageExhausted as error:
            self.record_rejection(event["source"], error, event["dedup_key"])
            raise
        return self.public(row) | {"duplicate": duplicate}

    def link_task(self, db, event_id, task_id):
        db.execute("UPDATE events SET task=? WHERE id=?", (task_id, event_id))

    @staticmethod
    def public(row):
        return {"id": row["id"], "source": row["source"], "kind": row["kind"], "persona": row["persona"],
                "occurred_at": row["occurred_at"], "received_at": row["received_at"], "task": row["task"],
                "correlation": row["correlation"]}

    def get(self, event_id, payload=False):
        row = self.store.one("SELECT * FROM events WHERE id=?", (event_id,))
        if row is None:
            return None
        out = self.public(row)
        if payload:
            out["payload"] = loads(row["payload"], {})
        return out

    def count(self, since=0):
        return self.store.one("SELECT COUNT(*) FROM events WHERE received_at>=?", (since,))[0]
