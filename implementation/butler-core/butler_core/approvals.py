"""Operation-bound, single-use approvals (REQ-023, REQ-024).

An approval names one operation, the hash of the parameters the approver
reviewed, the approver's identity, the channel and an expiry. It is consumed
inside the dispatch transaction, so it can authorize exactly one execution.
"""
from __future__ import annotations

import hashlib
import time

from . import contracts
from .store import dumps, new_id

CODE_LENGTH = 6


class ApprovalError(PermissionError):
    pass


def params_hash(kind, persona, target, params):
    canonical = dumps({"kind": kind, "persona": persona, "target": target, "params": params})
    return hashlib.sha256(canonical.encode()).hexdigest()


def review_code(digest):
    """Short code the approver echoes back; it changes whenever the parameters change."""
    return digest[:CODE_LENGTH]


class Approvals:
    def __init__(self, store, policy, diagnostics, clock=time.time, ttl=900):
        self.store, self.policy, self.diagnostics, self.clock, self.default_ttl = store, policy, diagnostics, clock, ttl

    def _ttl(self):
        _, content = self.policy.config.active()
        return (content or {}).get("approval", {}).get("ttl", self.default_ttl)

    def grant(self, operation_id, code, approver, channel):
        now = self.clock()
        with self.store.transaction(guard=False) as db:
            op = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if op is None:
                raise ApprovalError("operation not found")
            if op["state"] != "awaiting_approval":
                raise ApprovalError(f"operation is {op['state']}, not awaiting approval")
            if not self.policy.approver_allowed(op["persona"], approver):
                self.diagnostics.record("approval.refused", {"operation": operation_id, "reason": "approver"},
                                        op["correlation"], db=db)
                raise ApprovalError("approver is not authorized for this persona")
            if not isinstance(code, str) or code.lower() != review_code(op["params_hash"]):
                self.diagnostics.record("approval.refused", {"operation": operation_id, "reason": "code"},
                                        op["correlation"], db=db)
                raise ApprovalError("review code does not match the operation parameters")
            record = contracts.make("approval", id=new_id("ap"), operation=operation_id,
                                    params_hash=op["params_hash"], approver=approver, channel=channel,
                                    granted_at=now, expires_at=now + self._ttl())
            db.execute("INSERT INTO approvals VALUES(?,?,?,?,?,?,?,NULL,NULL)",
                       (record["id"], operation_id, record["params_hash"], approver, channel, now,
                        record["expires_at"]))
            db.execute("UPDATE operations SET state='requested', approval=?, updated_at=? WHERE id=?",
                       (record["id"], now, operation_id))
            self.diagnostics.record("approval.granted", {"operation": operation_id, "channel": channel},
                                    op["correlation"], db=db)
            wait = now - op["created_at"]
            self.diagnostics.observe("approval.wait", wait * 1000, {"kind": op["kind"]}, db=db)
        return record

    def reject(self, operation_id, approver, channel):
        now = self.clock()
        with self.store.transaction(guard=False) as db:
            op = db.execute("SELECT * FROM operations WHERE id=?", (operation_id,)).fetchone()
            if op is None or op["state"] != "awaiting_approval":
                raise ApprovalError("operation is not awaiting approval")
            if not self.policy.approver_allowed(op["persona"], approver):
                raise ApprovalError("approver is not authorized for this persona")
            db.execute("UPDATE operations SET state='cancelled', reason='rejected by approver', updated_at=?,"
                       " finished_at=? WHERE id=?", (now, now, operation_id))
            self.diagnostics.record("approval.rejected", {"operation": operation_id, "channel": channel},
                                    op["correlation"], db=db)
        return {"operation": operation_id, "state": "cancelled"}

    def consume(self, db, op):
        """Use the operation's approval once; raise when changed, expired, reused or revoked."""
        now = self.clock()
        row = db.execute("SELECT * FROM approvals WHERE id=?", (op["approval"],)).fetchone() if op["approval"] else None
        if row is None or row["operation"] != op["id"]:
            raise ApprovalError("no approval for this operation")
        if row["params_hash"] != op["params_hash"]:
            raise ApprovalError("parameters changed after review")
        if row["used_at"] is not None:
            raise ApprovalError("approval was already used")
        if row["revoked_at"] is not None:
            raise ApprovalError("approval was revoked")
        if row["expires_at"] <= now:
            raise ApprovalError("approval expired")
        if not self.policy.approver_allowed(op["persona"], row["approver"]):
            raise ApprovalError("approver authority was revoked")
        db.execute("UPDATE approvals SET used_at=? WHERE id=?", (now, row["id"]))
        return row["id"]

    def recheck(self, db, op):
        """A retry of the same operation after a certain non-dispatch reuses its consumed approval
        only while it is still unrevoked and its approver keeps authority."""
        row = db.execute("SELECT * FROM approvals WHERE id=?", (op["approval"],)).fetchone() if op["approval"] else None
        if row is None or row["operation"] != op["id"] or row["params_hash"] != op["params_hash"]:
            raise ApprovalError("no approval for this operation")
        if row["revoked_at"] is not None:
            raise ApprovalError("approval was revoked")
        if not self.policy.approver_allowed(op["persona"], row["approver"]):
            raise ApprovalError("approver authority was revoked")
        return row["id"]

    def revoke(self, operation_id, actor):
        now = self.clock()
        with self.store.transaction(guard=False) as db:
            changed = db.execute("UPDATE approvals SET revoked_at=? WHERE operation=? AND used_at IS NULL"
                                 " AND revoked_at IS NULL", (now, operation_id)).rowcount
            self.diagnostics.record("approval.revoked", {"operation": operation_id, "count": changed, "actor": actor},
                                    db=db)
        return {"operation": operation_id, "revoked": changed}
