"""Versioned configuration: validate → preview → activate → rollback (REQ-025).

Scenario switches, persona grants, export policy, operation rules and limits
all live in one ConfigVersion. An invalid or incompatible version is rejected
before it can become active.
"""
from __future__ import annotations

import hashlib
import re
import time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import contracts
from .store import dumps, loads

RAW_CATEGORIES = {"raw-observation", "raw-media", "biometric", "raw-transcript", "third-party-message"}
APPROVAL = ("none", "required")
RECONCILE = ("auto", "owner")
SYSTEM1 = ("off", "shadow", "active")
GRANT_KINDS = ("source", "action", "task", "persona")
_CLOCK = re.compile(r"([01]\d|2[0-3]):[0-5]\d")


class ConfigError(ValueError):
    pass


def _strings(value, where, limit=200):
    if not isinstance(value, list) or len(value) > limit or not all(isinstance(x, str) and x for x in value):
        raise ConfigError(f"{where}: list of names required")
    return value


def _number(value, where, low, high, integer=True):
    if type(value) not in ((int,) if integer else (int, float)) or not low <= value <= high:
        raise ConfigError(f"{where}: number {low}..{high} required")
    return value


def validate_content(content, adapters=None):
    """Raise ``ConfigError`` with the first problem; return the content unchanged."""
    if not isinstance(content, dict) or content.get("schema") != 1:
        raise ConfigError("schema: version 1 required")
    allowed = {"schema", "owner", "personas", "grants", "exports", "operations", "scenarios", "delivery", "limits",
               "bridge", "routing", "approval", "backup", "cron", "mail", "backburner", "system3"}
    unknown = sorted(set(content) - allowed)
    if unknown:
        raise ConfigError(f"{unknown[0]}: unknown section")
    owner = content.get("owner")
    if not isinstance(owner, dict):
        raise ConfigError("owner: section required")
    _strings(owner.get("principals"), "owner.principals")
    personas = content.get("personas")
    if not isinstance(personas, dict) or not personas:
        raise ConfigError("personas: at least one persona required")
    for name, persona in personas.items():
        if not contracts.ID.fullmatch(name) or not isinstance(persona, dict):
            raise ConfigError(f"personas.{name}: invalid persona")
        extra = sorted(set(persona) - {"principals", "approvers", "sources", "actions", "channels", "recipients",
                                       "description"})
        if extra:
            raise ConfigError(f"personas.{name}.{extra[0]}: unknown field")
        for field in ("principals", "approvers", "sources", "actions", "channels", "recipients"):
            _strings(persona.get(field, []), f"personas.{name}.{field}")
    for index, grant in enumerate(content.get("grants", [])):
        where = f"grants[{index}]"
        if not isinstance(grant, dict) or grant.get("persona") not in personas:
            raise ConfigError(f"{where}.persona: unknown persona")
        if grant.get("kind") not in GRANT_KINDS or not isinstance(grant.get("resource"), str) or not grant["resource"]:
            raise ConfigError(f"{where}: kind and resource required")
        if grant.get("by") not in owner["principals"] and grant.get("by") != "owner":
            raise ConfigError(f"{where}.by: grants are made only by the owner")
    for index, rule in enumerate(content.get("exports", [])):
        where = f"exports[{index}]"
        if not isinstance(rule, dict) or rule.get("persona") not in personas:
            raise ConfigError(f"{where}.persona: unknown persona")
        if not isinstance(rule.get("category"), str) or not isinstance(rule.get("recipient"), str):
            raise ConfigError(f"{where}: category and recipient required")
        if rule["category"] in RAW_CATEGORIES:
            raise ConfigError(f"{where}.category: raw observation materials stay local")
    operations = content.get("operations", {})
    if not isinstance(operations, dict):
        raise ConfigError("operations: mapping required")
    for kind, rule in operations.items():
        where = f"operations.{kind}"
        if not contracts.ID.fullmatch(kind) or not isinstance(rule, dict):
            raise ConfigError(f"{where}: invalid operation rule")
        if adapters is not None and rule.get("adapter") not in adapters:
            raise ConfigError(f"{where}.adapter: unknown adapter")
        if rule.get("approval", "none") not in APPROVAL or rule.get("reconcile", "auto") not in RECONCILE:
            raise ConfigError(f"{where}: approval none|required and reconcile auto|owner")
        for export in rule.get("export", []):
            if not isinstance(export, dict) or not isinstance(export.get("category"), str) \
                    or not isinstance(export.get("recipient"), str):
                raise ConfigError(f"{where}.export: category and recipient required")
            if export["category"] in RAW_CATEGORIES:
                raise ConfigError(f"{where}.export: raw observation materials stay local")
        _number(rule.get("max_attempts", 3), f"{where}.max_attempts", 1, 10)
        _number(rule.get("timeout", 600), f"{where}.timeout", 1, 86400)
        if "sources" in rule:
            _strings(rule["sources"], f"{where}.sources")
    for name, switch in content.get("scenarios", {}).items():
        if not isinstance(switch, dict) or type(switch.get("enabled")) is not bool:
            raise ConfigError(f"scenarios.{name}.enabled: true or false required")
    delivery = content.get("delivery", {})
    if delivery:
        quiet = delivery.get("quiet", [22, 8])
        if not (isinstance(quiet, list) and len(quiet) == 2 and all(type(h) is int and 0 <= h <= 23 for h in quiet)):
            raise ConfigError("delivery.quiet: [start hour, end hour] required")
        _number(delivery.get("max_attempts", 3), "delivery.max_attempts", 1, 10)
        _number(delivery.get("retry_window", 300), "delivery.retry_window", 0, 3600)
        for kind, limit in delivery.get("initiative", {}).items():
            _number(limit.get("per_day", 1), f"delivery.initiative.{kind}.per_day", 0, 100)
            _number(limit.get("interval", 0), f"delivery.initiative.{kind}.interval", 0, 86400)
    limits = content.get("limits", {})
    if limits:
        _number(limits.get("queue", 1000), "limits.queue", 1, 100000)
        for adapter, count in limits.get("workers", {}).items():
            _number(count, f"limits.workers.{adapter}", 1, 64)
    for account, rule in content.get("bridge", {}).get("accounts", {}).items():
        if not isinstance(rule, dict) or rule.get("persona") not in personas:
            raise ConfigError(f"bridge.accounts.{account}.persona: unknown persona")
    system1 = content.get("routing", {}).get("system1", {})
    if system1.get("mode", "shadow") not in SYSTEM1:
        raise ConfigError("routing.system1.mode: off|shadow|active")
    if system1.get("mode") == "active" and not system1.get("criteria_evidence"):
        raise ConfigError("routing.system1.mode: active requires agreed evaluation criteria evidence")
    _number(content.get("approval", {}).get("ttl", 900), "approval.ttl", 30, 7 * 86400)
    mail = content.get("mail", {})
    if not isinstance(mail, dict):
        raise ConfigError("mail: object required")
    for index, slot in enumerate(mail.get("times", [])):
        if not isinstance(slot, str) or len(slot.split(":")) != 2 or not all(part.isdigit() for part in slot.split(":")):
            raise ConfigError(f"mail.times[{index}]: HH:MM required")
    _number(mail.get("lookback_hours", 24), "mail.lookback_hours", 1, 24 * 14)
    backburner = content.get("backburner", {})
    if not isinstance(backburner, dict):
        raise ConfigError("backburner: object required")
    for field in ("time", "last_start"):
        slot = backburner.get(field, "00:30" if field == "time" else "06:00")
        if not isinstance(slot, str) or not _CLOCK.fullmatch(slot):
            raise ConfigError(f"backburner.{field}: HH:MM required")
    if backburner.get("time", "00:30") >= backburner.get("last_start", "06:00"):
        raise ConfigError("backburner.time: the night window must start before it ends")
    _number(backburner.get("max_runs", 5), "backburner.max_runs", 1, 20)
    try:
        ZoneInfo(backburner.get("zone", "America/Chicago"))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ConfigError("backburner.zone: a known time zone required") from None
    system3 = content.get("system3", {})
    if not isinstance(system3, dict):
        raise ConfigError("system3: object required")
    _number(system3.get("questions_per_week", 2), "system3.questions_per_week", 0, 20)
    _number(system3.get("analysis_interval", 21600), "system3.analysis_interval", 600, 7 * 86400)
    return content


def flatten(value, prefix=""):
    if isinstance(value, dict) and value:
        out = {}
        for key, item in value.items():
            out.update(flatten(item, f"{prefix}.{key}" if prefix else str(key)))
        return out
    return {prefix: value}


def diff(old, new):
    a, b = flatten(old or {}), flatten(new or {})
    return {"added": {k: b[k] for k in sorted(set(b) - set(a))},
            "removed": {k: a[k] for k in sorted(set(a) - set(b))},
            "changed": {k: {"from": a[k], "to": b[k]} for k in sorted(set(a) & set(b)) if a[k] != b[k]}}


class Config:
    def __init__(self, store, diagnostics, adapters=None, clock=time.time, on_activate=None):
        self.store, self.diagnostics, self.adapters, self.clock = store, diagnostics, adapters, clock
        self.on_activate = on_activate or (lambda version, content: None)
        self._active = None

    def _row(self, version):
        row = self.store.one("SELECT * FROM config_versions WHERE version=?", (version,))
        if row is None:
            raise ConfigError(f"version {version} not found")
        return row

    def active(self):
        """(version, content) of the active configuration; (0, None) before the first activation.

        Takes no lock of its own: callers may already hold the store lock inside a
        transaction, and a second lock here would invert the lock order."""
        number = int(self.store.meta("active_config", 0))
        cached = self._active
        if cached is not None and cached[0] == number:
            return cached
        cached = (number, loads(self._row(number)["content"]) if number else None)
        self._active = cached
        return cached

    def propose(self, content, author):
        validate_content(content, self.adapters)
        text = dumps(content)
        digest = hashlib.sha256(text.encode()).hexdigest()
        with self.store.transaction() as db:
            number = (db.execute("SELECT MAX(version) FROM config_versions").fetchone()[0] or 0) + 1
            record = contracts.make("config_version", id=f"cfg-{number}-{digest[:8]}", number=number,
                                    content=content, author=author, created_at=self.clock())
            db.execute("INSERT INTO config_versions VALUES(?,?,?,?,?,NULL,NULL,?)",
                       (number, record["id"], text, author, record["created_at"], digest))
        self.diagnostics.record("config.proposed", {"version": number, "author": author})
        return {"version": number, "diff": self.preview(number)}

    def preview(self, version):
        active_number, active = self.active()
        return diff(active, loads(self._row(version)["content"]))

    def activate(self, version, actor, _previous=None):
        content = loads(self._row(version)["content"])
        validate_content(content, self.adapters)
        current, _ = self.active()
        previous = (current or None) if _previous is None else (_previous or None)
        with self.store.transaction(guard=False) as db:
            db.execute("UPDATE config_versions SET activated_at=?, previous=? WHERE version=?",
                       (self.clock(), previous, version))
            db.execute("INSERT INTO meta VALUES('active_config',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (str(version),))
            self.diagnostics.record("config.activated", {"version": version, "previous": current, "actor": actor}, db=db)
        self._active = None
        self.on_activate(version, content)
        return {"version": version, "previous": current}

    def rollback(self, actor):
        current, _ = self.active()
        if not current:
            raise ConfigError("no active configuration")
        previous = self._row(current)["previous"]
        if not previous:
            raise ConfigError("no previous configuration to roll back to")
        target = self._row(previous)
        # Restoring the predecessor keeps its own predecessor, so repeated rollback walks back.
        return self.activate(previous, actor, _previous=target["previous"] or 0) | {"rolled_back": current}

    def versions(self):
        active, _ = self.active()
        return [{"version": r["version"], "author": r["author"], "created_at": r["created_at"],
                 "activated_at": r["activated_at"], "active": r["version"] == active}
                for r in self.store.query("SELECT version,author,created_at,activated_at FROM config_versions"
                                          " ORDER BY version")]

    def switches(self):
        """The scenario switches of the active version, for the switch file CLIs read (DEC-061)."""
        version, content = self.active()
        return {"version": version, "scenarios": {name: bool(rule.get("enabled"))
                                                  for name, rule in (content or {}).get("scenarios", {}).items()}}

    def enabled(self, scenario):
        _, content = self.active()
        return bool(content and content.get("scenarios", {}).get(scenario, {}).get("enabled"))
