"""DEC-065: choose important camera events after a committed observation.

Only already validated local facts are used. An event never grants identity,
consent or any action; the chosen frame stays local until owner deletion.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

ZONE = ZoneInfo("America/Chicago")
ENTITY_KINDS = {"person", "dog", "book"}
MIN_CONFIDENCE = .7
WINDOW_SECONDS = 600
DAILY_LIMIT = 20


def _confident(fact):
    value = fact.get("confidence")
    return type(value) in (int, float) and MIN_CONFIDENCE <= value <= 1


def entity_key(fact, corrections=None):
    """Kind plus inferred identity or book title, or kind plus unknown.

    An owner correction of the fact's subject replaces the inferred identity,
    as everywhere else facts are read.
    """
    identity = (corrections or {}).get(fact.get("subject"), fact.get("identity"))
    name = " ".join(str(identity or "").split()).casefold()[:80]
    return fact["kind"] + ":" + (name or "unknown")


def main_activity(facts):
    best = None
    for fact in facts:
        if fact.get("activity", "unknown") != "unknown" and _confident(fact):
            if best is None or fact["confidence"] > best["confidence"]: best = fact
    return None if best is None else best["activity"]


def important_events(facts, recent, last=None, corrections=None):
    """Return [(kind, key)] for new entities and a changed main activity.

    recent holds the fact lists of camera observations from the prior ten
    minutes; an entity is new when absent from all of them. last is the fact
    list of the immediately previous camera observation (any age), or None;
    the main known activity is an event when it differs from last's.
    """
    seen = {entity_key(fact, corrections) for prior in recent for fact in prior
            if fact.get("kind") in ENTITY_KINDS and _confident(fact)}
    events = []
    for fact in facts:
        if fact.get("kind") not in ENTITY_KINDS or not _confident(fact): continue
        event = (fact["kind"], entity_key(fact, corrections))
        if event[1] not in seen and event not in events: events.append(event)
    activity = main_activity(facts)
    if activity is not None and activity != main_activity(last or []):
        events.append(("activity", "activity:" + activity))
    return events


def local_day(at):
    return dt.datetime.fromtimestamp(at, ZONE).date().isoformat()
