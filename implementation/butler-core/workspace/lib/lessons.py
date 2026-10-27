"""Lessons — priority-weighted long-term rules.

Storage:
    LESSONS.md                       — full source of truth (all lessons, chronological by id)
    state/lessons-priority.json      — { lesson_id: { priority, last_used, first_seen } }

Priority rules:
    - confirm(id) → priority += 1, last_used = today
    - decay()     → priority -= 1 for lessons not used in 30 days (floor 0)
    - new lessons start at priority 1 (today's discovery)

Bootstrap injects top N (default 20) lessons as one-liners.
Agent reads full text on demand: butler-lesson get <id>.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Optional

WORKSPACE = Path("/Users/boris/.openclaw/workspace-butler")
LESSONS_FILE = WORKSPACE / "LESSONS.md"
PRIORITY_FILE = WORKSPACE / "state" / "lessons-priority.json"


def _canonical():
    from lib.butler_rules import RuleStore
    from lib.vault import vault_root
    return RuleStore(vault_root(), WORKSPACE)


def _load_priority() -> dict:
    if not PRIORITY_FILE.exists():
        return {}
    return json.loads(PRIORITY_FILE.read_text())


def _save_priority(p: dict) -> None:
    PRIORITY_FILE.parent.mkdir(parents=True, exist_ok=True)
    PRIORITY_FILE.write_text(json.dumps(p, indent=2, sort_keys=True))


def _parse_lessons() -> dict[str, dict]:
    """Parse LESSONS.md → {id: {title, body}}."""
    rules = _canonical()
    if rules is not None:
        from lib.knowledge_sources import authorized
        if not authorized():
            rules.block_context("vault-source-unavailable")
            return {}
        rules.publish()
        return {r["id"]: {"title": r["title"], "body": "## " + r["id"] + " — " + r["title"] + "\n\n" + r["text"]}
                for r in rules.scan().values() if r["status"] == "active"}
    if not LESSONS_FILE.exists():
        return {}
    text = LESSONS_FILE.read_text()
    out = {}
    # split on ## L-...
    parts = re.split(r'(?m)^(?=## L-)', text)
    for block in parts[1:]:
        m = re.match(r'^## (L-\d{4}-\d{2}-\d{2}-\d+)\s+—\s+(.+?)$', block.split('\n', 1)[0])
        if not m:
            continue
        lid = m.group(1)
        title = m.group(2).strip()
        out[lid] = {"title": title, "body": block.rstrip()}
    return out


def list_top(n: int = 20) -> list[dict]:
    """Return top-N lessons by priority (highest first, ties broken by recency of id)."""
    priorities = _load_priority()
    lessons = _parse_lessons()
    rows = []
    for lid, lesson in lessons.items():
        meta = priorities.get(lid, {"priority": 0, "last_used": None})
        rows.append({
            "id": lid,
            "priority": meta.get("priority", 0),
            "last_used": meta.get("last_used"),
            "title": lesson["title"],
        })
    # sort: priority DESC, then id DESC (more recent id wins ties)
    rows.sort(key=lambda r: r["id"], reverse=True)
    rows.sort(key=lambda r: -r["priority"])
    return rows[:n]


def get(lesson_id: str) -> Optional[str]:
    """Return full markdown body of a lesson."""
    lessons = _parse_lessons()
    if lesson_id in lessons:
        # Bump usage on read (optional — small reward for accessing)
        return lessons[lesson_id]["body"]
    return None


def confirm(lesson_id: str, *, by: float = 1.0) -> dict:
    """Mark lesson as useful; increment priority."""
    priorities = _load_priority()
    if lesson_id not in priorities:
        # New lesson — initialize
        priorities[lesson_id] = {
            "priority": 0,
            "last_used": None,
            "first_seen": dt.date.today().isoformat(),
        }
    cur = priorities[lesson_id]
    cur["priority"] = max(0, cur.get("priority", 0) + by)
    cur["last_used"] = dt.date.today().isoformat()
    _save_priority(priorities)
    return cur


def decay(within_days: int = 30) -> dict:
    """Decay priority by 1 for lessons not used in `within_days`."""
    priorities = _load_priority()
    today = dt.date.today()
    decayed = 0
    for lid, meta in priorities.items():
        last = meta.get("last_used")
        if last is None:
            continue
        try:
            last_date = dt.date.fromisoformat(last)
        except ValueError:
            continue
        if (today - last_date).days >= within_days and meta.get("priority", 0) > 0:
            meta["priority"] = max(0, meta["priority"] - 1)
            decayed += 1
    _save_priority(priorities)
    return {"decayed": decayed}


def add_new_lesson(lesson_id: str, title: str, body: str) -> str:
    """Append a new lesson block to LESSONS.md and initialize its priority."""
    rules = _canonical()
    if rules is not None:
        from lib.knowledge_sources import authorized
        if not authorized(): raise PermissionError("vault source unavailable")
        import hashlib
        operation = "lesson:" + lesson_id + ":" + hashlib.sha256(body.encode()).hexdigest()
        rows = rules.scan()
        rules.put(lesson_id, title, body, operation=operation, authorized=True, source="confirmed-system3", kind="lesson",
                  expected_hash=rows[lesson_id]["hash"] if lesson_id in rows else None)
        confirm(lesson_id)
        return lesson_id
    # Build the lesson block
    block = f"\n## {lesson_id} — {title}\n\n{body.rstrip()}\n"
    cur = LESSONS_FILE.read_text() if LESSONS_FILE.exists() else ""
    LESSONS_FILE.write_text(cur.rstrip() + "\n" + block)
    # initialize priority
    priorities = _load_priority()
    priorities[lesson_id] = {
        "priority": 1,
        "last_used": dt.date.today().isoformat(),
        "first_seen": dt.date.today().isoformat(),
    }
    _save_priority(priorities)
    return lesson_id


def retire(lesson_id: str) -> bool:
    """Remove a lesson block and its priority (System 3 retires or deletes a lesson; the bootstrap stops loading it)."""
    rules = _canonical()
    if rules is not None:
        if lesson_id not in rules.scan(): return False
        rules.change(lesson_id, operation="retire:" + lesson_id, authorized=True, status="retired")
        return True
    if not LESSONS_FILE.exists():
        return False
    text = LESSONS_FILE.read_text()
    pattern = re.compile(r'(?ms)^## ' + re.escape(lesson_id) + r'\s+—.*?(?=^## L-|\Z)')
    new_text, count = pattern.subn("", text)
    if count:
        LESSONS_FILE.write_text(new_text.rstrip() + "\n")
    priorities = _load_priority()
    if priorities.pop(lesson_id, None) is not None or count:
        _save_priority(priorities)
    return bool(count)


def search(query: str, *, in_body: bool = True) -> list[dict]:
    """Search titles (and optionally bodies) for keyword. Returns matches with priority."""
    q = query.lower()
    priorities = _load_priority()
    lessons = _parse_lessons()
    out = []
    for lid, lesson in lessons.items():
        title_hit = q in lesson["title"].lower()
        body_hit = in_body and q in lesson["body"].lower()
        if title_hit or body_hit:
            meta = priorities.get(lid, {"priority": 0, "last_used": None})
            out.append({
                "id": lid,
                "priority": meta.get("priority", 0),
                "title": lesson["title"],
                "matched_in": "title" if title_hit else "body",
            })
    out.sort(key=lambda r: (-r["priority"], r["id"]), reverse=False)
    out.sort(key=lambda r: (-r["priority"],))
    return out


def auto_confirm_from_sessions(*, within_hours: int = 24) -> dict:
    """Scan recent butler session jsonls for evidence that lessons were used.

    Heuristic: if a lesson's title keywords appear in agent's tool calls or
    text output within the last N hours, bump priority by 0.5 (half-confirm).

    Returns: { lesson_id: bumps_applied }
    """
    # A keyword match proves neither application nor compliance (REQ-061).
    return {"checked": 0, "confirmed": {}, "reason": "application receipts required"}
    import glob
    sessions_dir = Path("/Users/boris/.claude/projects/-Users-boris--openclaw-workspace-butler")
    if not sessions_dir.exists():
        return {"checked": 0, "confirmed": {}}

    cutoff = dt.datetime.now() - dt.timedelta(hours=within_hours)
    lessons = _parse_lessons()

    # Build keyword set per lesson from title (skip stopwords)
    STOPWORDS = {"the", "a", "an", "is", "in", "on", "at", "to", "for", "with",
                 "of", "and", "or", "as", "be", "by", "from", "that", "this",
                 "it", "if", "but", "what", "when", "your", "you", "i",
                 "should", "must", "via", "use", "uses"}
    lesson_keywords = {}
    for lid, lesson in lessons.items():
        # Extract significant words from title (>3 chars, not stopword)
        title = lesson["title"].lower()
        words = re.findall(r'[a-zA-Z][a-zA-Z0-9_-]{3,}', title)
        kws = [w for w in words if w.lower() not in STOPWORDS][:5]  # top 5
        lesson_keywords[lid] = set(kws)

    # Scan recent session content
    confirmed_bumps = {}
    checked = 0
    for sess_file in sorted(sessions_dir.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True):
        if dt.datetime.fromtimestamp(sess_file.stat().st_mtime) < cutoff:
            break  # sessions sorted by mtime; older = stop
        checked += 1
        try:
            content = sess_file.read_text(errors="ignore")[:1024*200]  # cap 200KB
        except Exception:
            continue
        # Check each lesson's keywords
        content_lower = content.lower()
        for lid, kws in lesson_keywords.items():
            if not kws:
                continue
            # Require at least 2 keyword hits to count
            hits = sum(1 for kw in kws if kw.lower() in content_lower)
            if hits >= 2:
                confirmed_bumps[lid] = confirmed_bumps.get(lid, 0) + 0.5

    # Apply bumps
    if confirmed_bumps:
        priorities = _load_priority()
        for lid, bump in confirmed_bumps.items():
            if lid not in priorities:
                priorities[lid] = {"priority": 0, "last_used": None, "first_seen": dt.date.today().isoformat()}
            priorities[lid]["priority"] = round(priorities[lid].get("priority", 0) + bump, 1)
            priorities[lid]["last_used"] = dt.date.today().isoformat()
        _save_priority(priorities)

    return {"checked": checked, "confirmed": confirmed_bumps}


def _main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__, file=sys.stderr)
        print("\nUsage:", file=sys.stderr)
        print("  python3 -m lib.lessons list [--n N]              (top N by priority)", file=sys.stderr)
        print("  python3 -m lib.lessons get <id>                  (full lesson text)", file=sys.stderr)
        print("  python3 -m lib.lessons confirm <id> [--by N]     (bump priority)", file=sys.stderr)
        print("  python3 -m lib.lessons decay [--days N]          (decay stale)", file=sys.stderr)
        return 2
    cmd = argv[0]
    if cmd == "context":
        import argparse, socket
        parser = argparse.ArgumentParser()
        parser.add_argument("--turn", required=True)
        args = parser.parse_args(argv[1:])
        if not re.fullmatch(r"[0-9a-f]{32}", args.turn): raise ValueError("invalid voice context reference")
        path = Path.home() / ".local/share/butler-ambient/ambient.sock"
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(5); client.connect(str(path))
            client.sendall((json.dumps({"command": "ui-rule-context", "args": {"turn": args.turn}}) + "\n").encode())
            with client.makefile("rb") as stream: response = json.loads(stream.readline(131073))
        if "error" in response: raise RuntimeError("voice rule context unavailable")
        print(json.dumps(response["result"], ensure_ascii=False)); return 0
    if cmd == "list":
        n = 20
        if len(argv) > 1 and argv[1] == "--n":
            n = int(argv[2])
        for r in list_top(n):
            stars = "★" * min(int(r["priority"]), 10)
            print(f"  {r['id']:18} {stars:<10}  {r['title']}")
        return 0
    elif cmd == "get":
        if len(argv) < 2:
            sys.stderr.write("usage: get <id>\n"); return 2
        body = get(argv[1])
        if body is None:
            sys.stderr.write(f"no such lesson: {argv[1]}\n"); return 1
        print(body)
        return 0
    elif cmd == "confirm":
        if len(argv) < 2:
            sys.stderr.write("usage: confirm <id> [--by N]\n"); return 2
        by = 1.0
        if "--by" in argv:
            by = float(argv[argv.index("--by") + 1])
        cur = confirm(argv[1], by=by)
        print(f"  {argv[1]}: priority={cur['priority']}, last_used={cur['last_used']}")
        return 0
    elif cmd == "decay":
        days = 30
        if "--days" in argv:
            days = int(argv[argv.index("--days") + 1])
        r = decay(within_days=days)
        print(f"  decayed {r['decayed']} lessons (not used in {days}+ days)")
        return 0
    elif cmd == "add":
        if len(argv) < 3:
            sys.stderr.write("usage: add <id> <title>   (body via stdin)\n"); return 2
        body = sys.stdin.read()
        lid = add_new_lesson(argv[1], argv[2], body)
        print(lid)
        return 0
    elif cmd == "retire":
        if len(argv) < 2:
            sys.stderr.write("usage: retire <id>\n"); return 2
        print("retired" if retire(argv[1]) else "not found")
        return 0
    elif cmd == "search":
        if len(argv) < 2:
            sys.stderr.write("usage: search 'keyword'\n"); return 2
        q = " ".join(argv[1:])
        matches = search(q)
        if not matches:
            print("(no matches)")
            return 0
        for r in matches:
            stars = "★" * min(int(r["priority"]), 5) if r["priority"] > 0 else "·"
            print(f"  {r['id']:18} {stars:<6} [{r['matched_in']:5}] {r['title']}")
        return 0
    elif cmd == "auto-confirm":
        hours = 24
        if "--hours" in argv:
            hours = int(argv[argv.index("--hours") + 1])
        r = auto_confirm_from_sessions(within_hours=hours)
        print(f"  scanned {r['checked']} session files (last {hours}h)")
        if not r["confirmed"]:
            print("  no lesson keyword matches found")
        else:
            print(f"  auto-confirmed {len(r['confirmed'])} lessons:")
            for lid, bump in sorted(r["confirmed"].items(), key=lambda x: -x[1]):
                print(f"    {lid}  +{bump}")
        return 0
    else:
        sys.stderr.write(f"unknown command: {cmd}\n")
        return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
