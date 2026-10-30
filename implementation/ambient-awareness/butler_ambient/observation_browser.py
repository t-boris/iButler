"""Owner-local browsing of what Butler concluded and when it could not conclude anything (ambient 0.8.0).

The panel's «Наблюдения → История» reads this. It shows the local, already derived facts of
``Store.history`` day by day, plus the gaps between them (pause, capture gaps, analysis down or
behind), so that an empty stretch always has a stated reason. It is a presentation of stored rows:

- Facts only: no transcripts and no media. Raw originals are deleted by the retention rules, which
  this module neither changes nor extends; it only reports whether an original still exists.
- Owner-local: the command is registered on the local socket for the panel; the MCP surface is a
  fixed list of tools that does not include it, and nothing here is passed to an agent, Telegram or
  the butler-core link.
- A pause hides image paths (the same rule as the gallery); times, sources and facts stay, because
  they are already on disk and visible in the existing «Последние наблюдения».
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re

from .event_frames import ZONE, local_day

SOURCES = ("camera", "screen", "microphone", "system-audio")
GLOBAL_GAP_SOURCES = {"all", "analysis"}       # gaps that concern every source
MAX_LIMIT = 200
DEFAULT_LIMIT = 100
MAX_OFFSET = 100_000
MERGE_GAP = {"capture-gap": 5.0}               # seconds between two events of one run; others use DEFAULT_MERGE_GAP
DEFAULT_MERGE_GAP = 120.0
STALLED_AFTER = 600.0                          # no new conclusion for this long while originals wait

_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_KEYS = {"day", "source", "empty", "offset", "limit"}


def _request(args):
    if not isinstance(args, dict) or set(args) - _KEYS:
        raise ValueError("invalid observation request")
    day = args.get("day")
    if day is not None:
        if not isinstance(day, str) or not _DAY.match(day):
            raise ValueError("invalid day")
        dt.date.fromisoformat(day)                      # a malformed calendar date raises ValueError
    source = args.get("source")
    if source is not None and source != "all" and source not in SOURCES:
        raise ValueError("invalid source")
    empty = args.get("empty", False)
    offset, limit = args.get("offset", 0), args.get("limit", DEFAULT_LIMIT)
    if type(empty) is not bool or type(offset) is not int or type(limit) is not int:
        raise ValueError("invalid paging")
    if not 0 <= offset <= MAX_OFFSET or not 1 <= limit <= MAX_LIMIT:
        raise ValueError("invalid paging")
    return {"day": day, "source": None if source == "all" else source, "empty": empty, "offset": offset, "limit": limit}


def day_bounds(day):
    """Epoch seconds of the local day ``day`` (a DST day is 23 or 25 hours long)."""
    first = dt.date.fromisoformat(day)
    start = dt.datetime.combine(first, dt.time(), tzinfo=ZONE)
    end = dt.datetime.combine(first + dt.timedelta(days=1), dt.time(), tzinfo=ZONE)
    return start.timestamp(), end.timestamp()


def _days(store, now):
    """Newest first: per local day, informative and empty observation counts and the number of gap events."""
    days = {}
    for row in store.execute("SELECT start, (facts='[]') AS empty FROM observations"):
        entry = days.setdefault(local_day(row["start"]), {"day": None, "informative": 0, "empty": 0, "gaps": 0})
        entry["empty" if row["empty"] else "informative"] += 1
    for row in store.execute("SELECT start FROM events"):
        entry = days.setdefault(local_day(row["start"]), {"day": None, "informative": 0, "empty": 0, "gaps": 0})
        entry["gaps"] += 1
    days.setdefault(local_day(now), {"day": None, "informative": 0, "empty": 0, "gaps": 0})   # today is always listed
    result = []
    for day in sorted(days, reverse=True):
        days[day]["day"] = day
        result.append(days[day])
    return result


def _merged_gaps(store, start, end, now):
    """Gap events of the window as runs: adjoining events of one source and reason become one row."""
    rows = store.execute("SELECT start, end, source, reason FROM events WHERE start<? AND COALESCE(end,?)>=? ORDER BY source, reason, start",
                         (end, now, start))
    runs = []
    for row in rows:
        row_end = row["end"] if row["end"] is not None else now
        run = runs[-1] if runs else None
        limit = MERGE_GAP.get(row["reason"], DEFAULT_MERGE_GAP)
        if run and run["source"] == row["source"] and run["reason"] == row["reason"] and row["start"] - run["end"] <= limit:
            run["end"] = max(run["end"], row_end)
            run["count"] += 1
            run["open"] = run["open"] or row["end"] is None
        else:
            runs.append({"type": "gap", "source": row["source"], "reason": row["reason"], "start": row["start"], "end": row_end,
                         "count": 1, "open": row["end"] is None})
    for run in runs:                                    # clip to the day so a run over midnight counts on each day
        run["start"], run["end"] = max(run["start"], start), min(run["end"], end)
    runs = [run for run in runs if run["end"] >= run["start"]]
    return sorted(runs, key=lambda run: run["start"], reverse=True)


def _coverage(runs):
    """Total seconds and events per gap reason for the day."""
    totals = {}
    for run in runs:
        entry = totals.setdefault(run["reason"], {"reason": run["reason"], "seconds": 0.0, "count": 0})
        entry["seconds"] += run["end"] - run["start"]
        entry["count"] += run["count"]
    return sorted(({**entry, "seconds": round(entry["seconds"], 1)} for entry in totals.values()), key=lambda e: -e["seconds"])


def analysis_state(store, now, pending=None):
    """Whether conclusions are still being produced: the last one, the originals waiting, and a stall flag."""
    last = store.execute("SELECT MAX(end) AS at FROM observations")[0]["at"]
    if pending is None:
        pending = store.execute("SELECT COUNT(*) AS n FROM media WHERE analyzed=0 AND path IS NOT NULL")[0]["n"]
    return {"last_at": last, "pending": pending, "stalled": pending > 0 and (last is None or now - last > STALLED_AFTER)}


def browse(service, args):
    request = _request(args)
    store = service.store
    now = service.clock()
    with service.lock:                                  # read-only: retention runs in the service loop, never from here
        days = _days(store, now)
        day = request["day"] or next((d["day"] for d in days if d["informative"] or d["empty"] or d["gaps"]), days[0]["day"])
        start, end = day_bounds(day)
        corrections = {r["subject"]: r["name"] for r in store.execute("SELECT * FROM corrections")}
        where, params = "o.end>=? AND o.start<?", [start, end]
        if request["source"]:
            where += " AND o.source=?"
            params.append(request["source"])
        rows = store.execute(f"""SELECT o.id, o.source, o.start, o.end, o.facts, m.preserved AS preserved,
                (m.path IS NOT NULL OR m.preserved IS NOT NULL) AS original FROM observations o
                LEFT JOIN media m ON m.id=o.media_id WHERE {where} ORDER BY o.start DESC, o.id DESC""", params)
        items, counts = [], {"informative": 0, "empty": 0}
        for row in rows:
            facts = json.loads(row["facts"])
            counts["informative" if facts else "empty"] += 1
            if not facts and not request["empty"]:
                continue
            for fact in facts:
                if fact.get("subject") in corrections:
                    fact["identity"], fact["identity_basis"] = corrections[fact["subject"]], "owner-confirmed"
            items.append({"type": "observation", "id": row["id"], "source": row["source"], "start": row["start"], "end": row["end"],
                          "facts": facts, "original": bool(row["original"]), "preserved": row["preserved"]})
        runs = _merged_gaps(store, start, end, now)
        if request["source"]:
            runs = [run for run in runs if run["source"] in (request["source"], *GLOBAL_GAP_SOURCES)]
        coverage = _coverage(runs)
        if request["empty"]:
            items = sorted(items + runs, key=lambda item: item["start"], reverse=True)
        total = len(items)
        page = items[request["offset"]:request["offset"] + request["limit"]]
        from .recordings import _allowed
        shown = _allowed(service)
        for item in page:
            preserved = item.pop("preserved", None)
            if item["type"] == "observation":
                item["frame"] = preserved if shown and preserved and os.path.isfile(preserved) else None
        return {"day": day, "timezone": str(ZONE), "days": days, "source": request["source"], "empty": request["empty"],
                "offset": request["offset"], "limit": request["limit"], "total": total, "items": page,
                "summary": {**counts, "gaps": coverage}, "analysis": analysis_state(store, now), "now": now}
