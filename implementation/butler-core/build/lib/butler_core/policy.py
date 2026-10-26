"""Persona isolation, owner grants and export policy (REQ-023, DEC-037, DEC-050).

Every check produces a recorded PolicyDecision at one of four boundaries:
search, export, execute and disclose. Personas are isolated by default; only
an owner grant in the active configuration opens a source, task or action to
another persona. A shared project association grants nothing.
"""
from __future__ import annotations

import time

from . import contracts
from .config import RAW_CATEGORIES
from .store import dumps, new_id


class Policy:
    def __init__(self, store, config, diagnostics, clock=time.time):
        self.store, self.config, self.diagnostics, self.clock = store, config, diagnostics, clock

    # -- configuration views -------------------------------------------------

    def _content(self):
        version, content = self.config.active()
        if not content:
            return 0, None
        return version, content

    def persona(self, name):
        _, content = self._content()
        return (content or {}).get("personas", {}).get(name)

    def owner(self, principal):
        _, content = self._content()
        return bool(content) and principal in content["owner"]["principals"]

    def _grants(self, content, persona, kind):
        return {g["resource"] for g in content.get("grants", []) if g["persona"] == persona and g["kind"] == kind}

    def principal_allowed(self, persona, principal):
        """Who may act in, or receive from, a persona."""
        _, content = self._content()
        rule = (content or {}).get("personas", {}).get(persona)
        if rule is None or principal is None:
            return False
        if principal.startswith("agent:"):
            return principal == f"agent:{persona}"
        if principal.startswith("system:"):
            return True
        return principal in content["owner"]["principals"] or principal in rule.get("principals", [])

    def approver_allowed(self, persona, principal):
        _, content = self._content()
        rule = (content or {}).get("personas", {}).get(persona)
        if rule is None or principal is None:
            return False
        return principal in content["owner"]["principals"] or principal in rule.get("approvers", [])

    # -- decisions -------------------------------------------------------------

    def _evaluate(self, content, persona, boundary, resource, principal, operation_kind):
        rule = content.get("personas", {}).get(persona)
        if rule is None:
            return "deny", ["persona-unknown"], [], "persona is not configured"
        if principal is not None and boundary in ("execute", "disclose", "search") \
                and not self.principal_allowed(persona, principal):
            return "deny", ["principal-not-in-persona"], [], "principal is not authorized for this persona"
        kind, _, name = resource.partition(":")
        if boundary == "search":
            scope = sorted(set(rule.get("sources", [])) | self._grants(content, persona, "source"))
            if kind == "source" and name in scope:
                return "allow", ["persona-source" if name in rule.get("sources", []) else "owner-grant"], scope, \
                    "source is in persona scope"
            return "deny", ["default-isolation"], scope, "source is outside persona scope"
        if boundary == "execute":
            actions = set(rule.get("actions", [])) | self._grants(content, persona, "action")
            if kind != "action" or name not in actions:
                return "deny", ["default-isolation"], [], "action is not granted to this persona"
            if name not in content.get("operations", {}):
                return "deny", ["operation-unconfigured"], [], "operation kind is not configured"
            return "allow", ["persona-action" if name in rule.get("actions", []) else "owner-grant"], [], \
                "action is granted"
        if boundary == "export":
            category, _, recipient = name.partition("@")
            if kind != "export" or not category or not recipient:
                return "deny", ["invalid-export"], [], "export resource must be export:<category>@<recipient>"
            if category in RAW_CATEGORIES:
                return "deny", ["raw-stays-local"], [], "raw observation materials stay local"
            for export in content.get("exports", []):
                if export["persona"] == persona and export["category"] == category and export["recipient"] == recipient:
                    return "allow", ["export-policy"], [], "category and recipient are approved"
            exception = content.get("operations", {}).get(operation_kind or "", {}).get("exception")
            if exception:
                return "approval_required", ["operation-exception"], [], \
                    "export needs operation-specific owner approval"
            return "deny", ["export-not-approved"], [], "category and recipient are not approved"
        if boundary == "disclose":
            if kind == "task":
                row = self.store.one("SELECT persona FROM tasks WHERE id=?", (name,))
                if row is None:
                    return "deny", ["task-unknown"], [], "task not found"
                if row["persona"] == persona:
                    return "allow", ["same-persona"], [], "task belongs to this persona"
                granted = self._grants(content, persona, "task") | {
                    f"{p}:*" for p in self._grants(content, persona, "persona")}
                if name in granted or f"{row['persona']}:*" in granted:
                    return "allow", ["owner-grant"], [], "owner granted access to this task"
                return "deny", ["default-isolation"], [], "task belongs to another persona"
            if kind == "recipient":
                if name in rule.get("recipients", []) or self.principal_allowed(persona, name):
                    return "allow", ["persona-recipient"], [], "recipient is authorized for this persona"
                return "deny", ["recipient-not-allowed"], [], "recipient is not authorized for this persona"
            return "deny", ["invalid-disclosure"], [], "unknown disclosure resource"
        return "deny", ["invalid-boundary"], [], "unknown boundary"

    def check(self, persona, boundary, resource, *, principal=None, operation=None, operation_kind=None,
              correlation=None, db=None):
        version, content = self._content()
        if content is None:
            outcome, rules, scope, reason = "deny", ["no-active-config"], [], "no active configuration"
        else:
            outcome, rules, scope, reason = self._evaluate(content, persona, boundary, resource, principal,
                                                          operation_kind)
        decision = contracts.make("policy_decision", id=new_id("pd"), persona=persona, principal=principal,
                                  boundary=boundary, resource=resource[:300], outcome=outcome, rules=rules,
                                  scope=scope[:50], reason=reason, at=self.clock(), config_version=version or 1)
        row = (decision["id"], persona, principal, boundary, decision["resource"], outcome, dumps(rules),
               dumps(decision["scope"]), reason, decision["at"], version, operation, correlation)
        sql = "INSERT INTO policy_decisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
        if db is not None:
            db.execute(sql, row)
        else:
            with self.store.transaction(guard=False) as tx:
                tx.execute(sql, row)
        return decision

    def sources(self, persona):
        """The search scope a persona may use right now."""
        _, content = self._content()
        rule = (content or {}).get("personas", {}).get(persona)
        if rule is None:
            return []
        return sorted(set(rule.get("sources", [])) | self._grants(content, persona, "source"))
