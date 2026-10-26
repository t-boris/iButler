"""Result delivery tracked separately from execution (REQ-024).

A failed notification never touches the completed operation: only delivery is
retried, within configured bounds. An ambiguous transmission is never blindly
repeated; it becomes ``uncertain`` unless the transport proves the retry is
deduplicated (same gateway instance, same idempotency key, inside its window).
Quiet hours and initiative limits apply to autonomous messages.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import os
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from . import contracts
from .adapters.base import NotDispatched, Rejected
from .store import new_id

DEFAULTS = {"zone": "America/Chicago", "quiet": [22, 8], "max_attempts": 3, "retry_window": 300,
            "initiative": {"advice": {"per_day": 3, "interval": 3600}, "daily": {"per_day": 1, "interval": 0}},
            # Factual status lines never ping (the behaviour butler-notify always had).
            "silent": ["status"]}
BACKOFF = (5, 20, 60)
STALE = {"advice": 120}
ATTACHMENT_LIMIT = 20 * 1024 * 1024
TERMINAL = ("delivered", "failed", "cancelled", "uncertain")


def settings(config):
    _, content = config.active()
    configured = (content or {}).get("delivery", {})
    merged = dict(DEFAULTS)
    merged.update(configured)
    merged["initiative"] = {**DEFAULTS["initiative"], **configured.get("initiative", {})}
    return merged


def in_quiet(now, rule):
    hour = dt.datetime.fromtimestamp(now, ZoneInfo(rule["zone"])).hour
    start, end = rule["quiet"]
    return hour >= start or hour < end if start > end else start <= hour < end


def quiet_end(now, rule):
    zone = ZoneInfo(rule["zone"])
    moment = dt.datetime.fromtimestamp(now, zone)
    end = moment.replace(hour=rule["quiet"][1], minute=0, second=0, microsecond=0)
    if end <= moment:
        end += dt.timedelta(days=1)
    return end.timestamp()


def summary(row):
    return {"id": row["id"], "channel": row["channel"], "kind": row["kind"], "persona": row["persona"],
            "state": row["state"], "attempts": row["attempts"], "receipt": row["receipt"], "reason": row["reason"],
            "idempotency_key": row["idempotency_key"], "content": row["content"][:1000],
            "task": row["task"], "operation": row["operation"], "created_at": row["created_at"],
            "updated_at": row["updated_at"]}


class Delivery:
    def __init__(self, store, config, policy, diagnostics, transports, clock=time.time, on_change=None):
        self.store, self.config, self.policy, self.diagnostics = store, config, policy, diagnostics
        self.transports, self.clock = transports, clock
        self.on_change = on_change or (lambda delivery_id, state: None)
        self.attachments = Path(store.root) / "attachments"

    def _stage(self, delivery_id, attachment):
        """Copy the file privately so the sent bytes cannot change after enqueue; return its record."""
        source = Path(attachment["path"])
        if source.is_symlink() or not source.is_file():
            raise ValueError("attachment must be a regular file")
        size = source.stat().st_size
        if size > ATTACHMENT_LIMIT:
            raise ValueError("attachment is larger than 20 MB")
        self.attachments.mkdir(mode=0o700, exist_ok=True)
        staged = self.attachments / f"{delivery_id}{source.suffix[:12]}"
        descriptor = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        digest = hashlib.sha256()
        with source.open("rb") as reader, os.fdopen(descriptor, "wb") as writer:
            for chunk in iter(lambda: reader.read(1 << 16), b""):
                digest.update(chunk)
                writer.write(chunk)
        return {"path": str(staged), "name": str(attachment.get("name") or source.name)[:120],
                "mime": str(attachment.get("mime") or "application/octet-stream")[:80], "size": size,
                "sha256": digest.hexdigest()}

    def _release(self, db, delivery_id):
        """Remove a staged file once its delivery has ended; its metadata stays for accounting."""
        row = db.execute("SELECT path FROM attachments WHERE delivery=?", (delivery_id,)).fetchone()
        if row is not None:
            Path(row["path"]).unlink(missing_ok=True)

    def enqueue(self, *, recipient, channel, persona, kind, content, idempotency_key, task=None, operation=None,
                correlation=None, created_at=None, attachment=None):
        now = self.clock()
        delivery_id = new_id("dl")
        record = contracts.make("delivery", id=delivery_id, recipient=recipient, channel=channel, persona=persona,
                                kind=kind, content=content, idempotency_key=idempotency_key, task=task,
                                operation=operation)
        correlation = correlation or delivery_id
        staged = result = None
        if attachment and self.store.one("SELECT 1 FROM deliveries WHERE idempotency_key=?",
                                         (idempotency_key,)) is None:
            staged = self._stage(delivery_id, attachment)
        try:
            result = self._enqueue(record, delivery_id, recipient, channel, persona, kind, idempotency_key, task,
                                   operation, correlation, created_at or now, now, staged)
            return result
        finally:
            # A duplicate, a denial or a failed write leaves no delivery to own the staged copy.
            if staged and (result or {}).get("id") != delivery_id:
                Path(staged["path"]).unlink(missing_ok=True)

    def _enqueue(self, record, delivery_id, recipient, channel, persona, kind, idempotency_key, task, operation,
                 correlation, created_at, now, staged):
        with self.store.transaction() as db:
            existing = db.execute("SELECT * FROM deliveries WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if existing is not None:
                return summary(existing) | {"duplicate": True}
            decision = self.policy.check(persona, "disclose", f"recipient:{recipient}", principal=recipient,
                                         correlation=correlation, db=db)
            if decision["outcome"] == "allow" and task:
                decision = self.policy.check(persona, "disclose", f"task:{task}", principal=recipient,
                                             correlation=correlation, db=db)
            if decision["outcome"] != "allow":
                self.diagnostics.record("delivery.denied", {"kind": kind, "reason": decision["reason"]}, correlation,
                                        db=db)
                return {"id": None, "state": "denied", "reason": decision["reason"]}
            db.execute("INSERT INTO deliveries VALUES(?,?,?,?,?,?,?,?,?,0,'pending',NULL,?,?,NULL,NULL,NULL,NULL,?,NULL)",
                       (delivery_id, recipient, channel, persona, kind, record["content"], idempotency_key, task,
                        operation, created_at, now, correlation))
            if staged:
                db.execute("INSERT INTO attachments VALUES(?,?,?,?,?,?)", (delivery_id, staged["path"], staged["name"],
                                                                          staged["mime"], staged["size"],
                                                                          staged["sha256"]))
            self.diagnostics.record("delivery.enqueued", {"kind": kind, "channel": channel}, correlation, db=db)
            row = db.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
        self.on_change(delivery_id, "pending")
        return summary(row)

    def ready(self, now=None):
        now = self.clock() if now is None else now
        return [r[0] for r in self.store.query(
            "SELECT id FROM deliveries WHERE state IN ('pending','deferred') AND (not_before IS NULL OR not_before<=?)"
            " ORDER BY created_at", (now,))]

    def _set(self, db, delivery_id, state, reason=None, **fields):
        assignments = ", ".join(f"{k}=?" for k in fields)
        db.execute(f"UPDATE deliveries SET state=?, reason=COALESCE(?,reason), updated_at=?"
                   f"{', ' + assignments if assignments else ''} WHERE id=?",
                   (state, reason, self.clock(), *fields.values(), delivery_id))
        if state in TERMINAL:
            self._release(db, delivery_id)

    def _gate(self, db, row, rule, now):
        """Quiet hours, staleness and initiative limits; return a blocking state or None."""
        kind = row["kind"]
        initiative = rule["initiative"]
        if kind in STALE and now - row["created_at"] > STALE[kind]:
            return "cancelled", "stale before delivery", {}
        if kind in initiative:
            # Keep the whole bounded attempt outside quiet hours.
            if in_quiet(now, rule) or in_quiet(now + 45, rule):
                if kind in STALE:
                    return "cancelled", "quiet hours", {}
                return "deferred", "quiet hours", {"not_before": quiet_end(now + 45, rule)}
            day = dt.datetime.fromtimestamp(now, ZoneInfo(rule["zone"])).date().isoformat()
            limit = initiative[kind]
            sent = db.execute("SELECT COUNT(*), MAX(updated_at) FROM deliveries WHERE kind=? AND day=? AND state IN"
                              " ('sending','delivered','uncertain') AND id!=?", (kind, day, row["id"])).fetchone()
            last = db.execute("SELECT MAX(updated_at) FROM deliveries WHERE kind=? AND state IN"
                              " ('sending','delivered','uncertain') AND id!=?", (kind, row["id"])).fetchone()[0]
            if sent[0] >= limit.get("per_day", 1) or (last is not None and now - last < limit.get("interval", 0)):
                return "cancelled", "initiative limit", {}
        return None

    def send(self, delivery_id):
        now = self.clock()
        rule = settings(self.config)
        with self.store.transaction(guard=False) as db:
            row = db.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
            if row is None or row["state"] not in ("pending", "deferred") or (row["not_before"] or 0) > now:
                return None if row is None else row["state"]
            decision = self.policy.check(row["persona"], "disclose", f"recipient:{row['recipient']}",
                                         principal=row["recipient"], correlation=row["correlation"], db=db)
            if decision["outcome"] == "allow" and row["task"]:
                decision = self.policy.check(row["persona"], "disclose", f"task:{row['task']}",
                                             principal=row["recipient"], correlation=row["correlation"], db=db)
            if decision["outcome"] != "allow":
                self._set(db, delivery_id, "cancelled", "disclosure revoked: " + decision["reason"])
                return "cancelled"
            gate = self._gate(db, row, rule, now)
            if gate is not None:
                state, reason, fields = gate
                self._set(db, delivery_id, state, reason, **fields)
                return state
            transport = self.transports.get(row["channel"])
            if transport is None:
                self._set(db, delivery_id, "failed", "no transport for channel")
                return "failed"
            day = dt.datetime.fromtimestamp(now, ZoneInfo(rule["zone"])).date().isoformat()
            self._set(db, delivery_id, "sending", None, attempts=row["attempts"] + 1,
                      first_attempt_at=row["first_attempt_at"] or now, gateway=transport.instance(), day=day)
            row = db.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
        message = {"id": row["id"], "recipient": row["recipient"], "channel": row["channel"],
                   "persona": row["persona"], "kind": row["kind"], "content": row["content"],
                   "idempotency_key": row["idempotency_key"],
                   "silent": in_quiet(now, rule) or row["kind"] in rule.get("silent", [])}
        staged = self.store.one("SELECT path, name, mime FROM attachments WHERE delivery=?", (delivery_id,))
        if staged is not None:
            message["attachment"] = {"path": staged["path"], "name": staged["name"], "mime": staged["mime"]}
        started = self.clock()
        try:
            receipt = transport.send(message)
        except NotDispatched as error:
            return self._after_failure(delivery_id, "not_dispatched", error.detail, rule)
        except Rejected as error:
            with self.store.transaction(guard=False) as db:
                self._set(db, delivery_id, "failed", f"rejected: {error.detail}")
            self.on_change(delivery_id, "failed")
            return "failed"
        except Exception as error:
            return self._after_failure(delivery_id, "ambiguous", getattr(error, "detail", type(error).__name__), rule)
        with self.store.transaction(guard=False) as db:
            self._set(db, delivery_id, "delivered", None, receipt=str(receipt)[:500])
            self.diagnostics.record("delivery.delivered", {"kind": row["kind"]}, row["correlation"], db=db)
            self.diagnostics.observe("delivery.send", (self.clock() - started) * 1000, {"channel": row["channel"]},
                                     db=db)
        self.on_change(delivery_id, "delivered")
        return "delivered"

    def _after_failure(self, delivery_id, outcome, detail, rule):
        now = self.clock()
        with self.store.transaction(guard=False) as db:
            row = db.execute("SELECT * FROM deliveries WHERE id=?", (delivery_id,)).fetchone()
            transport = self.transports.get(row["channel"])
            within = row["attempts"] < rule["max_attempts"] and now - row["first_attempt_at"] <= rule["retry_window"]
            if outcome == "not_dispatched":
                state = "pending" if within else "failed"
            else:
                same_gateway = transport is not None and getattr(transport, "deduplicates", False) \
                    and row["gateway"] is not None and transport.instance() == row["gateway"]
                state = "pending" if within and same_gateway else "uncertain"
            fields = {"not_before": now + BACKOFF[min(row["attempts"] - 1, len(BACKOFF) - 1)]} if state == "pending" else {}
            reason = {"pending": f"retry after {outcome}", "failed": f"{outcome}; retries exhausted",
                      "uncertain": "transmission outcome unknown; not retransmitted"}[state]
            self._set(db, delivery_id, state, reason, **fields)
            self.diagnostics.record("delivery." + state, {"after": outcome, "detail": detail}, row["correlation"],
                                    db=db)
        self.on_change(delivery_id, state)
        return state

    def observe(self, *, recipient, channel, persona, kind, receipt, idempotency_key, task=None, correlation=None):
        """Record a message the gateway delivered by itself (an agent reply)."""
        now = self.clock()
        with self.store.transaction(guard=False) as db:
            if db.execute("SELECT 1 FROM deliveries WHERE idempotency_key=?", (idempotency_key,)).fetchone():
                return {"duplicate": True}
            delivery_id = new_id("dl")
            db.execute("INSERT INTO deliveries VALUES(?,?,?,?,?,'',?,?,NULL,1,'observed',?,?,?,?,NULL,NULL,NULL,?,NULL)",
                       (delivery_id, recipient, channel, persona, kind, idempotency_key, task, receipt, now, now, now,
                        correlation or delivery_id))
        return {"id": delivery_id, "state": "observed"}

    def import_uncertain(self, entries):
        """Bring outcomes another system could not confirm into accounting, read-only: never resent.

        An entry is ``uncertain`` unless its evidence shows it was refused before
        transmission (``state`` = ``failed``)."""
        added = 0
        now = self.clock()
        with self.store.transaction(guard=False) as db:
            for entry in entries:
                key = "import:" + entry["key"]
                if db.execute("SELECT 1 FROM deliveries WHERE idempotency_key=?", (key,)).fetchone():
                    continue
                state = "failed" if entry.get("state") == "failed" else "uncertain"
                delivery_id = new_id("dl")
                db.execute("INSERT INTO deliveries VALUES(?,?,?,?,?,'',?,NULL,NULL,?,?,NULL,?,?,?,NULL,"
                           "NULL,?,?,NULL)",
                           (delivery_id, entry.get("recipient", "unknown"), entry.get("channel", "unknown"),
                            entry.get("persona", "unknown"), entry.get("kind", "imported"), key,
                            int(entry.get("attempts", 0)), state, entry.get("created_at", now), now,
                            entry.get("created_at", now), entry.get("reason", "imported from gateway failed queue"),
                            delivery_id))
                added += 1
        return {"imported": added}

    def recover(self):
        """A delivery interrupted mid-transmission is ambiguous: same rules as a lost response."""
        rule = settings(self.config)
        return {r[0]: self._after_failure(r[0], "ambiguous", "core restarted during transmission", rule)
                for r in self.store.query("SELECT id FROM deliveries WHERE state='sending'")}

    def list(self, states=None, limit=50):
        sql, params = "SELECT * FROM deliveries", []
        if states:
            sql += " WHERE state IN (%s)" % ",".join("?" * len(states))
            params.extend(states)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        return [summary(r) for r in self.store.query(sql, params)]
