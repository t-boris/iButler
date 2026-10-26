"""Versioned shared contracts (REQ-021).

Every record that crosses a module or process boundary is a plain JSON object
``{"contract": <kind>, "version": <n>, ...fields}``. ``validate`` accepts only a
supported version and the exact field set of that version; anything else is
rejected with a ``ContractError`` whose reason is stored with the rejection.
"""
from __future__ import annotations

import json
import math
import re

ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._@/+=-]{0,199}")
HASH = re.compile(r"[0-9a-f]{64}")
PAYLOAD_LIMIT = 64 * 1024

TASK_STATES = ("accepted", "scheduled", "awaiting_approval", "running", "succeeded", "failed",
               "cancelled", "needs_reconciliation", "interrupted")
STEP_STATES = ("pending", "ready", "running", "awaiting_approval", "succeeded", "failed",
               "cancelled", "needs_reconciliation", "interrupted", "superseded")
OPERATION_STATES = ("requested", "awaiting_approval", "dispatching", "running", "succeeded", "failed",
                    "needs_reconciliation", "cancelled")
DELIVERY_STATES = ("pending", "deferred", "sending", "delivered", "failed", "uncertain",
                   "cancelled", "observed")
BOUNDARIES = ("search", "export", "execute", "disclose")


class ContractError(ValueError):
    """Input that does not match a supported contract version."""

    def __init__(self, reason, field=None):
        super().__init__(reason if field is None else f"{field}: {reason}")
        self.reason, self.field = reason, field


def _id(value):
    return isinstance(value, str) and ID.fullmatch(value) is not None


def _time(value):
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _text(limit):
    return lambda value: isinstance(value, str) and len(value) <= limit


def _enum(*values):
    return lambda value: value in values


def _int(low, high):
    return lambda value: type(value) is int and low <= value <= high


def _list(item, limit):
    return lambda value: isinstance(value, list) and len(value) <= limit and all(item(x) for x in value)


def _object(limit=PAYLOAD_LIMIT):
    def check(value):
        if not isinstance(value, dict):
            return False
        try:
            return len(json.dumps(value, ensure_ascii=False, allow_nan=False)) <= limit
        except (TypeError, ValueError):
            return False
    return check


def _hash(value):
    return isinstance(value, str) and HASH.fullmatch(value) is not None


# name: (check, required, default). A default of None means absent.
SCHEMAS = {
    "event": {1: {
        "id": (_id, True, None), "source": (_id, True, None), "kind": (_id, True, None),
        "occurred_at": (_time, True, None), "received_at": (_time, True, None),
        "persona": (_id, True, None), "conversation": (_id, False, None), "principal": (_id, False, None),
        "dedup_key": (_text(300), True, None),
        "privacy": (_enum("raw_local_only", "summary_ok", "public"), True, None),
        "payload": (_object(), False, {}), "refs": (_list(_id, 50), False, []),
        "correlation": (_id, False, None),
    }},
    "conversation": {1: {
        "id": (_id, True, None), "persona": (_id, True, None),
        "participants": (_list(_id, 20), True, None), "scope": (_list(_id, 50), False, []),
        "channels": (_list(_id, 20), False, []), "tasks": (_list(_id, 500), False, []),
    }},
    "task": {1: {
        "id": (_id, True, None), "kind": (_id, True, None), "goal": (_text(2000), True, None),
        "initiator": (_id, True, None), "persona": (_id, True, None), "state": (_enum(*TASK_STATES), True, None),
        "created_at": (_time, True, None), "updated_at": (_time, True, None), "deadline": (_time, False, None),
        "config_version": (_int(1, 10**9), True, None), "events": (_list(_id, 100), False, []),
        "conversation": (_id, False, None), "reason": (_text(500), False, None),
    }},
    "task_step": {1: {
        "id": (_id, True, None), "task": (_id, True, None), "index": (_int(0, 999), True, None),
        "executor": (_id, True, None), "action": (_id, True, None), "depends_on": (_list(_id, 50), False, []),
        "attempts": (_int(0, 100), False, 0), "max_attempts": (_int(1, 20), False, 3),
        "state": (_enum(*STEP_STATES), True, None), "input": (_object(), False, {}),
        "result": (_object(), False, None), "cancel": (_object(2048), False, None), "operation": (_id, False, None),
    }},
    "operation": {1: {
        "id": (_id, True, None), "kind": (_id, True, None), "persona": (_id, True, None),
        "principal": (_id, False, None), "task": (_id, False, None), "step": (_id, False, None),
        "params": (_object(), True, None), "target": (_id, True, None),
        "idempotency_key": (_text(300), True, None), "export": (_object(1024), False, None),
        "receipt": (_text(500), False, None), "state": (_enum(*OPERATION_STATES), False, "requested"),
        "attempts": (_int(0, 100), False, 0), "max_attempts": (_int(1, 20), False, 3),
    }},
    "policy_decision": {1: {
        "id": (_id, True, None), "persona": (_id, True, None), "principal": (_id, False, None),
        "boundary": (_enum(*BOUNDARIES), True, None), "resource": (_text(300), True, None),
        "outcome": (_enum("allow", "deny", "approval_required"), True, None),
        "rules": (_list(_text(200), 20), False, []), "scope": (_list(_id, 50), False, []),
        "reason": (_text(500), True, None), "at": (_time, True, None), "config_version": (_int(1, 10**9), True, None),
    }},
    "approval": {1: {
        "id": (_id, True, None), "operation": (_id, True, None), "params_hash": (_hash, True, None),
        "approver": (_id, True, None), "channel": (_id, True, None), "granted_at": (_time, True, None),
        "expires_at": (_time, True, None), "used_at": (_time, False, None),
    }},
    "evidence_ref": {1: {
        "id": (_id, True, None), "source": (_id, True, None), "at": (_time, True, None),
        "access": (_list(_id, 20), True, None),
        "kind": (_enum("observation", "message", "document", "session", "task", "inference", "canonical"), True, None),
        "available": (_enum("available", "unavailable", "deleted"), True, None), "uri": (_text(1000), False, None),
    }},
    "delivery": {1: {
        "id": (_id, True, None), "recipient": (_id, True, None), "channel": (_id, True, None),
        "persona": (_id, True, None), "kind": (_id, True, None), "content": (_text(4096), True, None),
        "idempotency_key": (_text(300), True, None), "task": (_id, False, None), "operation": (_id, False, None),
        "attempts": (_int(0, 100), False, 0), "state": (_enum(*DELIVERY_STATES), False, "pending"),
        "receipt": (_text(500), False, None),
    }},
    "config_version": {1: {
        "id": (_id, True, None), "number": (_int(1, 10**9), True, None), "content": (_object(512 * 1024), True, None),
        "author": (_id, True, None), "created_at": (_time, True, None), "activated_at": (_time, False, None),
        "previous": (_int(1, 10**9), False, None),
    }},
}
SUPPORTED = {kind: tuple(sorted(versions)) for kind, versions in SCHEMAS.items()}


def validate(data, expected=None):
    """Return a normalized copy of ``data`` or raise ``ContractError``."""
    if not isinstance(data, dict):
        raise ContractError("contract object required")
    kind, version = data.get("contract"), data.get("version")
    if kind not in SCHEMAS:
        raise ContractError("unknown contract", "contract")
    if expected is not None and kind != expected:
        raise ContractError(f"expected {expected}", "contract")
    if type(version) is not int or version not in SCHEMAS[kind]:
        raise ContractError(f"unsupported version; supported {list(SUPPORTED[kind])}", "version")
    fields = SCHEMAS[kind][version]
    unknown = sorted(set(data) - set(fields) - {"contract", "version"})
    if unknown:
        raise ContractError("unknown field for this version", unknown[0])
    result = {"contract": kind, "version": version}
    for name, (check, required, default) in fields.items():
        if name not in data or data[name] is None:
            if required:
                raise ContractError("required", name)
            if default is not None:
                result[name] = json.loads(json.dumps(default))
            continue
        if not check(data[name]):
            raise ContractError("invalid value", name)
        result[name] = data[name]
    return result


def make(_contract, **fields):
    """Build and validate the current version of a contract."""
    return validate({"contract": _contract, "version": SUPPORTED[_contract][-1], **fields}, _contract)
