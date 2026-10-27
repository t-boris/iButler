"""Back Burner researcher: nightly, in-place improvement of long-running research folders.

A folder under `<vault>/Back Burner/<Name>/` holds markdown notes and a `_brief.md` (the owner's spec).
Once a night every active folder gets one run:

    preflight -> snapshot -> stage copy -> confined Opus worker -> validate -> conflict check -> apply
    -> record -> notify

The model only ever sees and edits a STAGING COPY (`state/backburner/work/<run>/folder`). Everything that
reaches the vault is decided by deterministic code in this module and written through `lib.vault`
(policy-checked, snapshotted, audited, and routed through butler-core while the `vault` scenario is on).

Guarantees the code is built around (each has a test that fails when the check is removed):
  * `_brief.md` is never changed by a run; a file is never deleted or renamed by a run;
  * a document that would lose more than `shrink` of its text is refused (the researcher's version is kept
    in `_researcher/rejected/<run>/`);
  * a file the owner edited while the run was going is never overwritten (the researcher's version goes to
    `_researcher/conflicts/<run>/`);
  * a run without a journal entry for today and a valid `run-summary.json` applies nothing;
  * every run can be undone: files it edited get their pre-run text back (if still as the run left them),
    files it created are MOVED aside to `_researcher/undone/<run>/`, nothing is ever deleted.

CLI (also reachable as ./bin/butler-backburner):
    list | new "<Name>" --goal "..." | pause|resume|done <name> | run <name> [--dry-run]
    cycle [--only <name>] [--dry-run] [--id <cycle id>] | status <name> | history <name> | undo <name> [--run <id>]
"""
from __future__ import annotations

import argparse
import contextlib
import dataclasses
import datetime as dt
import difflib
import fcntl
import fnmatch
import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tarfile
import time
import unicodedata
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator, Optional
from zoneinfo import ZoneInfo

from lib import vault

# ───────────────────────────────────────────────────────────────────
# Paths and contract
# ───────────────────────────────────────────────────────────────────
WORKSPACE = Path("/Users/boris/.openclaw/workspace-butler")
STATE = WORKSPACE / "state" / "backburner"
LOG = WORKSPACE / "logs" / "backburner.jsonl"
PROMPT_FILE = WORKSPACE / "state" / "backburner-worker-prompt.txt"
AMBIENT_SOCKET = Path("/Users/boris/.local/share/butler-ambient/ambient.sock")   # the display lives in the ambient service
DISPLAY_TIMEOUT = 30                                                              # opening the panel window can take a few seconds

SUBDIR = "Back Burner"
BRIEF = "_brief.md"
RESEARCHER = "_researcher"
JOURNAL = f"{RESEARCHER}/journal.md"
SOURCES = f"{RESEARCHER}/sources.md"
SUMMARY = f"{RESEARCHER}/run-summary.json"      # staging only: read by the harness, never applied
CONTEXT = f"{RESEARCHER}/.context.md"           # staging only: read-only context for the worker
SIDE_DIRS = (f"{RESEARCHER}/undone", f"{RESEARCHER}/rejected", f"{RESEARCHER}/conflicts")
APPEND_ONLY_LOGS = (JOURNAL, SOURCES)           # history: a run may add to them, not drop old entries
FROM = "backburner"                             # `from:` of the write.yaml rules for Back Burner/**
# Vault areas whose notes may be shown to the (cloud) worker as related context. Daily notes, people,
# mail and the like stay out on purpose; widen this list only on the owner's word.
CONTEXT_ROOTS = ("Research/AI Generated Research/", "Knowledge/", "Goals/")
UF_DATALESS = 0x40000000                        # iCloud placeholder (file is not on disk)

NAME_OK = re.compile(r"^[\w][\w .,()&+'-]{0,79}$")
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


@dataclasses.dataclass(frozen=True)
class Limits:
    max_files: int = 200
    max_bytes: int = 5_000_000
    max_file_bytes: int = 300_000
    max_docs_changed: int = 3        # of the owner's existing documents, per run
    max_new_docs: int = 5
    shrink: float = 0.35             # a document may lose at most this share of its characters
    keep_snapshots: int = 30
    max_runs: int = 5                # per night
    run_timeout: int = 2700
    stall_after: int = 3             # consecutive runs without progress before backing off
    stall_days: int = 7              # ... and the back-off ends after this many days anyway
    window_start: str = "00:30"
    window_end: str = "06:00"        # no new run starts at or after this local time
    zone: str = "America/Chicago"
    placeholder_budget: int = 300    # seconds to wait for iCloud downloads of one folder


DEFAULT = Limits()
CLOCK = re.compile(r"([01]\d|2[0-3]):[0-5]\d")
CYCLE_ID = re.compile(r"bb-[0-9]{8}-[0-9]{6}-[a-f0-9]{6}")


def limits_from(window_start: Any = None, window_end: Any = None, max_runs: Any = None, base: Limits = DEFAULT) -> Limits:
    """The nightly limits with the core's schedule applied. A value that cannot be used is refused, never guessed."""
    changes: dict[str, Any] = {}
    for name, value in (("window_start", window_start), ("window_end", window_end)):
        if value is not None:
            if not isinstance(value, str) or not CLOCK.fullmatch(value):
                raise BackburnerError(f"{name}: HH:MM required")
            changes[name] = value
    if max_runs is not None:
        if type(max_runs) is not int or not 1 <= max_runs <= 20:
            raise BackburnerError("max_runs: a number from 1 to 20 required")
        changes["max_runs"] = max_runs
    limits = dataclasses.replace(base, **changes)
    if _minutes(limits.window_start) >= _minutes(limits.window_end):
        raise BackburnerError("the night window must start before it ends")
    return limits


def effective_limits() -> Limits:
    """What the last core-started cycle ran with (the core's config is the owner's setting); defaults before that."""
    saved = _read_json(STATE / "schedule.json", {})
    try:
        return limits_from(saved.get("window_start"), saved.get("window_end"), saved.get("max_runs"))
    except (BackburnerError, AttributeError):
        return DEFAULT


class BackburnerError(Exception):
    """A refusal the caller can show as is."""


# ───────────────────────────────────────────────────────────────────
# Small helpers
# ───────────────────────────────────────────────────────────────────
def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _flat(rel: str) -> str:
    return rel.replace("/", "__")


def _clean(value: Any, limit: int) -> str:
    """One line of text from a model-written value: no control characters, bounded."""
    text = re.sub(r"\s+", " ", CONTROL.sub(" ", str(value))).strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def slug_of(name: str) -> str:
    """Stable, filesystem-safe id of a folder name (readable part plus a short hash, so near-twins differ)."""
    name = unicodedata.normalize("NFC", name)
    readable = re.sub(r"[^\w]+", "-", name.lower()).strip("-")[:40] or "folder"
    return f"{readable}-{hashlib.sha1(name.encode()).hexdigest()[:6]}"


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=1, sort_keys=True))
    os.replace(temporary, path)


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def _log(event: dict) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as stream:
        stream.write(json.dumps({"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), **event},
                                ensure_ascii=False) + "\n")


@contextlib.contextmanager
def _locked(slug: str) -> Iterator[bool]:
    """One run per folder at a time; yields False when another process holds the folder."""
    path = STATE / "locks" / f"{slug}.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        yield False
        return
    try:
        yield True
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


# ───────────────────────────────────────────────────────────────────
# Reading a folder
# ───────────────────────────────────────────────────────────────────
def _ignored(rel: str, ignore: tuple[str, ...]) -> bool:
    """Whether the brief's `ignore:` list covers a path: an entry (exact or glob) matches the path itself or
    one of its parent folders."""
    parts = rel.split("/")
    parents = ["/".join(parts[:count]) for count in range(1, len(parts) + 1)]
    return any(entry == parent or fnmatch.fnmatch(parent, entry) for entry in ignore for parent in parents)


def clean_paths(raw: Any) -> tuple[str, ...]:
    """A brief's `ignore:` or `readonly:` list reduced to safe relative entries. An entry that would cover the
    brief or the researcher's own files is dropped: the harness must always see (and the researcher write) those."""
    items = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    kept = []
    for item in items:
        if not isinstance(item, str):
            continue
        entry = item.strip().strip("/")
        entry = entry[2:] if entry.startswith("./") else entry
        if not entry or len(entry) > 100 or ".." in entry.split("/") or CONTROL.search(entry):
            continue
        if _ignored(BRIEF, (entry,)) or _ignored(JOURNAL, (entry,)) or _ignored(SUMMARY, (entry,)):
            continue
        kept.append(entry)
    return tuple(kept[:20])


def _walk(root: Path, ignore: tuple[str, ...] = ()) -> Iterator[tuple[str, Path, str]]:
    """(relative path, path, kind) for everything under root. kind: "md" regular markdown, "link" a symlink,
    "other" any other file. Hidden entries, the researcher's side folders and the brief's ignored paths are
    skipped, symlinks never followed."""
    for current, dirs, names in os.walk(root, followlinks=False):
        here = Path(current)
        rel_dir = here.relative_to(root).as_posix()
        rel_dir = "" if rel_dir == "." else rel_dir
        kept = []
        for directory in sorted(dirs):
            rel = f"{rel_dir}/{directory}".lstrip("/")
            if directory.startswith(".") or rel in SIDE_DIRS or _ignored(rel, ignore):
                continue
            if (here / directory).is_symlink():
                yield rel, here / directory, "link"
                continue
            kept.append(directory)
        dirs[:] = kept
        for name in sorted(names):
            path, rel = here / name, f"{rel_dir}/{name}".lstrip("/")
            if name.startswith(".") or _ignored(rel, ignore):
                continue
            if path.is_symlink():
                yield rel, path, "link"
            elif path.is_file() and name.endswith(".md"):
                yield rel, path, "md"
            else:
                yield rel, path, "other"


def read_folder(root: Path, ignore: tuple[str, ...] = ()) -> tuple[dict[str, bytes], list[tuple[str, str]]]:
    """Markdown contents by relative path, plus (relative path, kind) of everything else that was seen."""
    contents: dict[str, bytes] = {}
    other: list[tuple[str, str]] = []
    for rel, path, kind in _walk(root, ignore):
        if kind == "md":
            contents[rel] = path.read_bytes()
        else:
            other.append((rel, kind))
    return contents, other


def is_placeholder(path: Path) -> bool:
    try:
        return bool(os.lstat(path).st_flags & UF_DATALESS)
    except (OSError, AttributeError):
        return False


def _brctl_download(path: Path) -> None:
    subprocess.run(["brctl", "download", str(path)], capture_output=True, timeout=30)


def preflight(root: Path, limits: Limits = DEFAULT, *, ignore: tuple[str, ...] = (), placeholder: Callable[[Path], bool] = is_placeholder,
              download: Callable[[Path], None] = _brctl_download, sleep: Callable[[float], None] = time.sleep,
              clock: Callable[[], float] = time.monotonic) -> str:
    """Why this folder cannot run tonight, or "" when it can. iCloud placeholders are downloaded first: a file
    that stays a placeholder means skip the folder, never treat it as empty or as done."""
    files = [(rel, path) for rel, path, kind in _walk(root, ignore) if kind == "md"]
    if len(files) > limits.max_files:
        return f"too many files ({len(files)} > {limits.max_files})"
    total = 0
    for rel, path in files:
        size = os.lstat(path).st_size
        total += size
        if size > limits.max_file_bytes:
            return f"{rel} is too large ({size} bytes > {limits.max_file_bytes})"
    if total > limits.max_bytes:
        return f"folder is too large ({total} bytes > {limits.max_bytes})"
    waiting = [path for _, path in files if placeholder(path)]
    deadline = clock() + limits.placeholder_budget
    for path in waiting:
        with contextlib.suppress(Exception):
            download(path)
    while waiting and clock() < deadline:
        sleep(5)
        waiting = [path for path in waiting if placeholder(path)]
    if waiting:
        return f"{len(waiting)} file(s) are still iCloud placeholders (first: {waiting[0].name})"
    for rel, path in files:
        try:
            path.read_bytes().decode("utf-8")
        except UnicodeDecodeError:
            return f"{rel} is not UTF-8 text"
    return ""


def stat_signature(root: Path, ignore: tuple[str, ...] = ()) -> str:
    """Cheap fingerprint of the folder (names, sizes, mtimes), no file is read: used to notice owner edits."""
    rows = []
    for rel, path, kind in _walk(root, ignore):
        if kind == "md":
            stat = os.lstat(path)
            rows.append(f"{rel}\0{stat.st_size}\0{stat.st_mtime_ns}")
    return hashlib.sha256("\n".join(rows).encode()).hexdigest()


# ───────────────────────────────────────────────────────────────────
# Brief, folder state, discovery
# ───────────────────────────────────────────────────────────────────
@dataclasses.dataclass
class Brief:
    status: str = ""
    priority: int = 3
    focus: str = ""
    ignore: tuple[str, ...] = ()      # paths the researcher never sees, edits or counts (e.g. a MarkView project's machinery)
    readonly: tuple[str, ...] = ()    # paths it may read but whose every change is refused (e.g. decisions, raw source notes)


def read_brief(folder: Path) -> Optional[Brief]:
    """None when the folder has no `_brief.md` (it is not a research folder)."""
    path = folder / BRIEF
    if not path.is_file():
        return None
    try:
        fields = vault._frontmatter_fields(path.read_text(errors="replace"))
    except OSError:
        fields = {}
    try:
        priority = min(5, max(1, int(fields.get("priority", 3))))
    except (TypeError, ValueError):
        priority = 3
    return Brief(status=str(fields.get("status") or "").strip().lower(), priority=priority,
                 focus=_clean(fields.get("focus") or "", 300), ignore=clean_paths(fields.get("ignore")),
                 readonly=clean_paths(fields.get("readonly")))


@dataclasses.dataclass
class Folder:
    name: str
    path: Path
    slug: str
    brief: Brief
    state: dict


def _state_path(slug: str) -> Path:
    return STATE / "folders" / f"{slug}.json"


def load_state(slug: str) -> dict:
    return _read_json(_state_path(slug), {})


def save_state(slug: str, value: dict) -> None:
    _write_json(_state_path(slug), value)


def back_burner_root() -> Path:
    return vault.vault_root() / SUBDIR


def discover() -> list[Folder]:
    """Every folder under Back Burner/ that has a `_brief.md`, in nightly order: priority (1 first), then the
    one waiting longest, then name."""
    base = back_burner_root()
    if not base.is_dir():
        return []
    found = []
    for child in sorted(base.iterdir()):
        if child.name.startswith((".", "_")) or child.is_symlink() or not child.is_dir():
            continue
        brief = read_brief(child)
        if brief is None:
            continue
        name = unicodedata.normalize("NFC", child.name)
        slug = slug_of(name)
        found.append(Folder(child.name, child, slug, brief, load_state(slug)))
    return sorted(found, key=lambda f: (f.brief.priority, f.state.get("last_run_at", ""), f.name))


def find(token: str, folders: Optional[list[Folder]] = None) -> Folder:
    """A folder by exact name, slug, or unique case-insensitive name prefix."""
    folders = discover() if folders is None else folders
    token_nfc = unicodedata.normalize("NFC", token)
    for folder in folders:
        if unicodedata.normalize("NFC", folder.name) == token_nfc or folder.slug == token:
            return folder
    near = [f for f in folders if unicodedata.normalize("NFC", f.name).casefold().startswith(token_nfc.casefold())]
    if len(near) == 1:
        return near[0]
    raise BackburnerError(f"no unique folder matches {token!r}" + (f" ({', '.join(f.name for f in near)})" if near else ""))


def waiting_reason(folder: Folder, now: dt.datetime, limits: Limits = DEFAULT) -> str:
    """Why a folder sits out tonight, or "" when it is due."""
    if folder.brief.status != "active":
        return f"status is {folder.brief.status or 'missing in the brief'}"
    state = folder.state
    if state.get("stalled_runs", 0) >= limits.stall_after:
        last = state.get("last_run_at")
        old = not last or (now - dt.datetime.fromisoformat(last)).days >= limits.stall_days
        if not old and stat_signature(folder.path, folder.brief.ignore) == state.get("signature"):
            return f"{state['stalled_runs']} runs without progress; waiting for your edits"
    return ""


# ───────────────────────────────────────────────────────────────────
# Snapshot and staging
# ───────────────────────────────────────────────────────────────────
def snapshot(slug: str, run_id: str, contents: dict[str, bytes], limits: Limits = DEFAULT) -> Path:
    """The pre-run text of every file, as one archive. Undo restores from it; old ones are pruned."""
    directory = STATE / "snapshots" / slug
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{run_id}.tar.gz"
    temporary = target.with_name(target.name + ".tmp")
    with tarfile.open(temporary, "w:gz") as archive:
        for rel in sorted(contents):
            info = tarfile.TarInfo(rel)
            info.size = len(contents[rel])
            info.mtime = int(time.time())
            archive.addfile(info, io.BytesIO(contents[rel]))
    os.replace(temporary, target)
    for old in sorted(directory.glob("*.tar.gz"))[: -limits.keep_snapshots]:
        old.unlink()
    return target


def load_snapshot(path: Path) -> dict[str, bytes]:
    with tarfile.open(path, "r:gz") as archive:
        return {member.name: archive.extractfile(member).read() for member in archive.getmembers() if member.isfile()}


def stage(run_id: str, contents: dict[str, bytes], context: str) -> Path:
    """The worker's only writable place: a copy of the folder plus read-only context."""
    shutil.rmtree(STATE / "work" / run_id, ignore_errors=True)       # never build on a leftover copy
    work = STATE / "work" / run_id / "folder"
    for rel, data in contents.items():
        target = work / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    (work / RESEARCHER).mkdir(parents=True, exist_ok=True)
    (work / CONTEXT).write_text(context)
    return work


def sweep_work(max_age_days: int = 3, now: Optional[float] = None) -> None:
    """Staging copies are kept a few days for inspection, then removed."""
    base = STATE / "work"
    cutoff = (now or time.time()) - max_age_days * 86400
    for child in base.iterdir() if base.is_dir() else []:
        if child.stat().st_mtime < cutoff:
            shutil.rmtree(child, ignore_errors=True)


def _recall(query: str, k: int) -> list:
    from lib import memory
    return memory.recall(query, k=k, scope=("vault",))


def build_context(folder: Folder, recall: Callable[[str, int], list] = _recall) -> str:
    """Related notes the vault already holds, restricted to CONTEXT_ROOTS; never part of the applied result."""
    brief_text = ""
    with contextlib.suppress(OSError):
        brief_text = (folder.path / BRIEF).read_text(errors="replace")
    query = _clean(f"{folder.name} {re.sub(r'^---.*?---', '', brief_text, flags=re.S)}", 600)
    try:
        hits = recall(query, 40)
    except Exception:
        hits = []
    lines = ["# Related notes already in the vault (read-only context)", ""]
    kept = 0
    for hit in hits:
        path = str(getattr(hit, "path", ""))
        if getattr(hit, "scope", "vault") != "vault" or not path.startswith(CONTEXT_ROOTS):
            continue
        lines.append(f"- `{path}`: {_clean(getattr(hit, 'snippet', ''), 300)}")
        kept += 1
        if kept == 8:
            break
    if not kept:
        lines.append("(nothing related found)")
    return "\n".join(lines) + "\n"


# ───────────────────────────────────────────────────────────────────
# Validation: what the worker's staging copy may change
# ───────────────────────────────────────────────────────────────────
@dataclasses.dataclass
class Change:
    rel: str
    kind: str                 # "create" | "edit"
    text: str
    base_sha: Optional[str]


@dataclasses.dataclass
class Rejection:
    rel: str
    reason: str
    text: Optional[str] = None      # the researcher's version, kept for the owner to review


@dataclasses.dataclass
class Plan:
    changes: list[Change] = dataclasses.field(default_factory=list)
    rejected: list[Rejection] = dataclasses.field(default_factory=list)
    summary: dict = dataclasses.field(default_factory=dict)
    fatal: str = ""


def parse_summary(path: Path) -> Optional[dict]:
    """The worker's `run-summary.json`, or None when it is missing or malformed."""
    try:
        if path.is_symlink():
            return None
        raw = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("improved"), list) or not isinstance(raw.get("blocked_on_owner"), bool):
        return None

    def count(key: str) -> int:
        value = raw.get(key, 0)
        return min(max(value, 0), 999) if isinstance(value, int) and not isinstance(value, bool) else 0

    return {"improved": [_clean(item, 140) for item in raw["improved"] if isinstance(item, str)][:5],
            "key_fact": _clean(raw.get("key_fact", ""), 200), "questions": count("questions"),
            "sources_added": count("sources_added"), "blocked_on_owner": raw["blocked_on_owner"],
            "scores": raw["scores"] if isinstance(raw.get("scores"), dict) else {}}


def _diff_size(old: str, new: str) -> int:
    return sum(1 for line in difflib.unified_diff(old.splitlines(), new.splitlines(), lineterm="", n=0)
               if line[:1] in "+-" and line[:3] not in ("+++", "---"))


def validate(base: dict[str, bytes], staged_root: Path, today: dt.date, limits: Limits = DEFAULT,
             ignore: tuple[str, ...] = (), readonly: tuple[str, ...] = ()) -> Plan:
    """Compare the staging copy with the pre-run text and decide, file by file, what may be applied. A file the
    worker puts in an ignored path is never seen here, so it is never applied; any change under a read-only
    path is refused."""
    plan = Plan()
    staged, other = read_folder(staged_root, ignore)
    for rel, kind in other:
        if kind == "link":
            plan.fatal = f"symlink in the staging copy: {rel}"
            return plan
    summary = parse_summary(staged_root / SUMMARY)
    if summary is None:
        plan.fatal = "no valid _researcher/run-summary.json"
        return plan
    plan.summary = summary
    journal_now, journal_before = staged.get(JOURNAL), base.get(JOURNAL)
    if journal_now is None or journal_now == journal_before or today.isoformat() not in journal_now.decode("utf-8", "replace"):
        plan.fatal = "the journal has no entry for today"
        return plan

    owned_edits: list[tuple[int, str, Change]] = []
    new_documents = 0
    for rel in sorted(set(base) | set(staged)):
        old, new = base.get(rel), staged.get(rel)
        mine = rel.startswith(f"{RESEARCHER}/")
        if rel == BRIEF:
            if new != old:
                plan.rejected.append(Rejection(rel, "the brief belongs to the owner"))
            continue
        if _ignored(rel, readonly):
            if new != old:
                plan.rejected.append(Rejection(rel, "this path is read-only for the researcher"))
            continue
        if new is None:
            plan.rejected.append(Rejection(rel, "deleting or renaming a file is not allowed"))
            continue
        if new == old:
            continue
        if CONTROL.search(rel) or len(rel) > 160:
            plan.rejected.append(Rejection(rel, "unusable file name"))
            continue
        text = new.decode("utf-8", "replace")
        if len(new) > limits.max_file_bytes:
            plan.rejected.append(Rejection(rel, f"file is larger than {limits.max_file_bytes} bytes"))
            continue
        if old is None:
            if not mine:
                new_documents += 1
                if new_documents > limits.max_new_docs:
                    plan.rejected.append(Rejection(rel, f"more than {limits.max_new_docs} new documents in one run", text))
                    continue
            plan.changes.append(Change(rel, "create", text, None))
            continue
        before = old.decode("utf-8", "replace")
        if text.split() == before.split():
            continue                                    # whitespace only
        if (not mine or rel in APPEND_ONLY_LOGS) and len(text) < len(before) * (1 - limits.shrink):
            plan.rejected.append(Rejection(rel, f"would remove more than {int(limits.shrink * 100)}% of the text", text))
            continue
        change = Change(rel, "edit", text, _sha(old))
        if mine:
            plan.changes.append(change)
        else:
            owned_edits.append((_diff_size(before, text), rel, change))
    owned_edits.sort(key=lambda item: (-item[0], item[1]))
    for _, rel, change in owned_edits[limits.max_docs_changed:]:
        plan.rejected.append(Rejection(rel, f"more than {limits.max_docs_changed} of your documents in one run", change.text))
    plan.changes.extend(change for _, _, change in owned_edits[: limits.max_docs_changed])
    return plan


# ───────────────────────────────────────────────────────────────────
# Applying through lib.vault
# ───────────────────────────────────────────────────────────────────
def stamp(text: str, run_id: str) -> str:
    """Mark a file the harness creates: `managed_by` lets vault.move() file it away on undo, `bb_created` says which run."""
    keys = f"managed_by: ai-butler\nbb_created: {run_id}"
    match = vault.FRONTMATTER_RE.match(text)
    if not match:
        return f"---\n{keys}\n---\n\n{text}"
    body = re.sub(r"^(managed_by|bb_created):.*\n?", "", match.group(1), flags=re.M).rstrip("\n")
    return f"---\n{body}\n{keys}\n---\n" + text[match.end():]


def _meta(run_id: str) -> dict:
    return {"source": f"backburner:{run_id}", "source_metadata": True, "from": FROM}


@dataclasses.dataclass
class Applied:
    rel: str
    kind: str                       # "create" | "edit"
    before_sha: Optional[str]
    after_sha: str
    role: str = "document"          # "document" | "conflict" | "rejected"


def _vault_rel(name: str, rel: str) -> str:
    return f"{SUBDIR}/{name}/{rel}"


def _write_file(name: str, rel: str, text: str, kind: str, run_id: str) -> None:
    write = vault.edit if kind == "edit" else vault.create
    result = write(_vault_rel(name, rel), text, **_meta(run_id))
    if not result.ok:
        raise BackburnerError(f"the vault policy did not allow {kind} on {rel} ({result.action})")


def apply_plan(folder: Folder, plan: Plan, base: dict[str, bytes], run_id: str,
               record: Optional[Callable[[list[Applied]], None]] = None) -> tuple[list[Applied], list[str], str]:
    """Write what validation accepted. Returns (applied, conflicts, error). Each file is listed in `applied` (and
    handed to `record`) BEFORE it is written, so a crash mid-write still leaves a record to recover from. On a
    write failure everything already applied is put back (see revert) and the error says so."""
    applied: list[Applied] = []
    conflicts: list[str] = []

    def put(rel: str, text: str, kind: str, before_sha: Optional[str], role: str) -> None:
        applied.append(Applied(rel, kind, before_sha, _sha(text.encode()), role))
        if record:
            record(applied)
        try:
            _write_file(folder.name, rel, text, kind, run_id)
        except Exception:
            applied.pop()
            raise

    try:
        for change in sorted(plan.changes, key=lambda c: (c.rel.startswith(f"{RESEARCHER}/"), c.rel)):
            live = folder.path / change.rel
            exists = live.is_file()
            clash = ""
            if change.kind == "edit":
                if not exists or is_placeholder(live):
                    clash = "the file is gone or not on disk"
                elif _sha(live.read_bytes()) != change.base_sha:
                    clash = "you edited it while the run was going"
            elif exists:
                clash = "a file with this name appeared during the run"
            if clash:
                conflicts.append(change.rel)
                put(f"{RESEARCHER}/conflicts/{run_id}/{_flat(change.rel)}", stamp(change.text, run_id), "create", None, "conflict")
            elif change.kind == "create":
                put(change.rel, stamp(change.text, run_id), "create", None, "document")
            else:
                put(change.rel, change.text, "edit", change.base_sha, "document")
        for item in plan.rejected:
            if item.text is not None:
                put(f"{RESEARCHER}/rejected/{run_id}/{_flat(item.rel)}", stamp(item.text, run_id), "create", None, "rejected")
    except Exception as error:
        problems = revert(folder, applied, base, run_id)
        message = f"{type(error).__name__}: {error}"[:300]
        return [], conflicts, message + (f"; could not put back: {', '.join(problems)}" if problems else "; everything applied was put back")
    return applied, conflicts, ""


def revert(folder: Folder, applied: list[Applied], base: dict[str, bytes], run_id: str) -> list[str]:
    """Put back what a run applied: edited files get their pre-run text (only if still as the run left them),
    created files are moved to `_researcher/undone/<run>/`. Returns the files that could not be put back."""
    problems: list[str] = []
    for item in reversed(applied):
        live = folder.path / item.rel
        try:
            if item.kind == "create" and not live.exists():
                continue                                    # the write never happened
            current = _sha(live.read_bytes()) if live.is_file() else None
            if item.kind == "edit" and current == item.before_sha:
                continue                                    # the write never happened
            if current != item.after_sha:
                problems.append(f"{item.rel} (changed since)")
                continue
            if item.kind == "edit":
                result = vault.edit(_vault_rel(folder.name, item.rel), base[item.rel].decode("utf-8"), **_meta(f"{run_id}:undo"))
                if not result.ok:
                    problems.append(f"{item.rel} (policy)")
            else:
                vault.move(_vault_rel(folder.name, item.rel),
                           _vault_rel(folder.name, f"{RESEARCHER}/undone/{run_id}/{item.rel}"), reason="backburner undo")
        except Exception as error:
            problems.append(f"{item.rel} ({type(error).__name__})")
    return problems


# ───────────────────────────────────────────────────────────────────
# Worker, notes
# ───────────────────────────────────────────────────────────────────
@dataclasses.dataclass
class WorkerResult:
    success: bool
    exit_code: int = 0
    text: str = ""
    error: str = ""
    cost_usd: float = 0.0
    duration: float = 0.0


def dispatch_worker(prompt: str, cwd: Path, timeout: int) -> WorkerResult:
    """The confined `backburner` route of lib/dispatch.py: runs in this process group, so a core cancel stops it."""
    from lib import dispatch
    result = dispatch.dispatch("backburner", str(cwd), prompt, timeout=timeout)
    return WorkerResult(result.success, result.exit_code, result.stdout or "", (result.stderr or "")[-300:],
                        result.estimated_cost_usd, result.duration_sec)


def build_prompt(folder: Folder, run_number: int, days_since: Optional[int], today: dt.date, refused: tuple[str, ...] = ()) -> str:
    """The fixed instructions plus harness-made parameters. Nothing from the folder is put on the command line;
    the only worker-influenced text is a few sanitized file names the last run had refused."""
    try:
        base = PROMPT_FILE.read_text()
    except OSError:
        raise BackburnerError(f"worker prompt missing: {PROMPT_FILE}") from None
    name = re.sub(r"[^\w .,()&+'-]", "", folder.name)[:80]
    since = "first run" if days_since is None else str(days_since)
    params = [f"date: {today.isoformat()}", f"run: {run_number}", f"folder: {name}", f"days_since_last_run: {since}"]
    if refused:
        params.append("last_run_refused: " + "; ".join(re.sub(r"[^\w .,;()/&+'-]", "", item)[:120] for item in refused[:5]))
    return f"{base.rstrip()}\n\nRUN PARAMETERS\n" + "\n".join(params) + "\n"


def _last_refused(slug: str) -> tuple[str, ...]:
    """What the checks refused in this folder's previous finished run, so the worker does not propose it again."""
    finished = [r for r in runs_of(slug) if r.get("status") in ("applied", "noop")]
    return tuple(f"{item['rel']} ({item['reason']})" for item in (finished[-1].get("rejected", []) if finished else []))

def render_note(folder: Folder, run_number: int, summary: dict, applied: list[Applied], conflicts: list[str],
                rejected: int, backing_off: bool = False) -> str:
    """The short Russian Telegram note. Plain text: Telegram does not render tables (L-2026-05-21-14)."""
    lines = [f"🔥 {folder.name} · день {run_number}"]
    documents = [a for a in applied if a.role == "document" and not a.rel.startswith(f"{RESEARCHER}/")]
    if documents and summary.get("improved"):
        lines.append("Улучшено: " + "; ".join(summary["improved"][:3]))
    elif not documents:
        lines.append("Документы не менялись.")
    if summary.get("key_fact"):
        lines.append(f"Главное: {summary['key_fact']}")
    if summary.get("questions"):
        lines.append(f"Вопросов к тебе: {summary['questions']} (_researcher/questions.md)")
    if conflicts:
        lines.append(f"Ты правил файл во время прогона: {len(conflicts)} (версия исследователя в _researcher/conflicts)")
    if rejected:
        lines.append(f"Отклонено проверкой: {rejected} (см. _researcher/rejected)")
    if backing_off:
        lines.append("Пауза: несколько прогонов без прогресса. Вернусь, когда ты что-то изменишь.")
    lines.append(f"📁 {SUBDIR}/{folder.name}")
    text = "\n".join(lines)
    return text if len(text) <= 700 else text[:699].rstrip() + "…"


def _notify(text: str, key: str) -> str:
    """Queue the note through core delivery (quiet hours and dedup live there). Returns "" or the error."""
    try:
        from lib import butler_core_client as core
        core.Client().notify(text, key, kind="research")
        return ""
    except Exception as error:
        return f"{type(error).__name__}: {error}"[:200]


_MARKDOWN_SPECIAL = re.compile(r"([\\`*_\[\]<>#])")


def _md(value: Any, limit: int) -> str:
    """Model- or file-written text as inert Markdown. The display renders CommonMark and makes links clickable, so
    a page the worker read must not be able to put a link, a heading or an image on it: every character that
    starts markup gets a backslash, which CommonMark shows as the plain character."""
    text = _MARKDOWN_SPECIAL.sub(r"\\\1", _clean(value, limit))
    return re.sub(r"^(\d+)\.", r"\1\\.", re.sub(r"^([-+=])", r"\\\1", text))


def _score(scores: Any) -> str:
    """"2.9 → 3.4" from the worker's folder score, or "" unless both are plain numbers on the 0-5 scale."""
    pair = scores.get("folder") if isinstance(scores, dict) else None
    if not isinstance(pair, dict):
        return ""
    before, after = pair.get("before"), pair.get("after")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 5 for v in (before, after)):
        return ""                                                  # NaN fails the range test too
    return f"{before:.1f} → {after:.1f}"


def render_card(folder: Folder, run_number: int, summary: dict, applied: list[Applied], conflicts: list[str],
                rejected: int, backing_off: bool = False) -> dict:
    """The display card of one run (DEC-006): the same facts as the Telegram note, laid out for reading and with
    buttons for the folder. Markdown, because the display renders it; everything the model wrote goes through `_md`."""
    documents = [a for a in applied if a.role == "document" and not a.rel.startswith(f"{RESEARCHER}/")]
    parts = []
    if summary.get("key_fact"):
        parts.append(f"**Главное**\n\n{_md(summary['key_fact'], 200)}")
    if documents and summary.get("improved"):
        parts.append("**Улучшено**\n\n" + "\n".join(f"- {_md(item, 140)}" for item in summary["improved"][:5]))
    elif not documents:
        parts.append("Документы не менялись.")
    facts = []
    if _score(summary.get("scores")):
        facts.append(f"Оценка папки: {_score(summary.get('scores'))}")
    if summary.get("sources_added"):
        facts.append(f"Источников добавлено: {summary['sources_added']}")
    if summary.get("questions"):
        facts.append(f"Вопросов к тебе: {summary['questions']} (questions\\.md в папке \\_researcher)")
    if conflicts:
        facts.append(f"Ты правил файл во время прогона: {len(conflicts)} (версия исследователя в \\_researcher/conflicts)")
    if rejected:
        facts.append(f"Отклонено проверкой: {rejected} (см\\. \\_researcher/rejected)")
    if backing_off:
        facts.append("Пауза: несколько прогонов без прогресса. Вернусь, когда ты что-то изменишь.")
    if facts:
        parts.append("\n\n".join(facts))
    if documents:
        names = [_md(a.rel, 120) for a in documents]
        shown = "\n".join(f"- {name}" for name in names[:8]) + (f"\n- и ещё {len(names) - 8}" if len(names) > 8 else "")
        parts.append(f"**Изменено**\n\n{shown}")
    parts.append(f"{SUBDIR}/{_md(folder.name, 80)}")
    name = _clean(folder.name, 40)
    buttons = [{"label": "Папка в Finder", "kind": "reveal", "path": str(folder.path)}]
    if (folder.path / RESEARCHER / "status.md").is_file():
        buttons.append({"label": "Показать status.md", "kind": "reveal", "path": str(folder.path / RESEARCHER / "status.md")})
    buttons.append({"label": f"Покажи журнал «{name}»", "kind": "reply"})
    return {"kind": "text", "title": f"🔥 {_clean(folder.name, 80)} · день {run_number}", "markdown": "\n\n".join(parts), "buttons": buttons}


def _present(card: dict) -> tuple:
    """Show a card on the Butler display through the ambient service socket, the way the butler_display tool does.
    Returns (card id, "") or (None, why): the display being down or refusing a card is a report, not an error."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(DISPLAY_TIMEOUT)
            client.connect(str(AMBIENT_SOCKET))
            client.sendall(json.dumps({"command": "display", "args": card}).encode() + b"\n")
            with client.makefile("rb") as stream:
                reply = json.loads(stream.readline(1_000_000))
    except (OSError, ValueError) as error:
        return None, f"{type(error).__name__}: {error}"[:200]
    if not isinstance(reply, dict):
        return None, "unreadable reply"
    if "error" in reply:
        return None, f"error: {reply['error']}"[:200]
    result = reply.get("result")
    if isinstance(result, dict) and result.get("shown") and isinstance(result.get("id"), str):
        return result["id"], ""
    return None, f"refused: {result.get('refused') if isinstance(result, dict) else result}"[:200]


def _show(folder: Folder, state: dict, card: dict, present: Callable[[dict], tuple]) -> str:
    """Show the folder's card: the next run redraws the earlier card in place instead of adding another. The display
    forgets its cards when the ambient service restarts, so a card that is gone is shown again as a new one.
    Returns "" or why nothing was shown; never raises, so a finished run stays finished."""
    try:
        earlier = state.get("display_card")
        card_id, error = present({**card, "replaces": earlier} if earlier else card)
        if card_id is None and earlier and "replaces: no such card" in error:
            card_id, error = present(card)
        if card_id is not None:
            state["display_card"] = card_id
            save_state(folder.slug, state)
        return "" if card_id is not None else error
    except Exception as error:
        return f"{type(error).__name__}: {error}"[:200]


# ───────────────────────────────────────────────────────────────────
# One run, one night
# ───────────────────────────────────────────────────────────────────
def _run_path(run_id: str) -> Path:
    return STATE / "runs" / f"{run_id}.json"


def new_run_id(now: dt.datetime) -> str:
    return now.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


def _index(name: str, applied: list[Applied]) -> None:
    """Make the changed notes searchable now instead of at the nightly reindex. Best effort."""
    try:
        from lib import embeddings
        for item in applied:
            if item.role == "document":
                embeddings.index_file(_vault_rel(name, item.rel))
    except Exception:
        pass


def run_folder(folder: Folder, *, now: Optional[dt.datetime] = None, limits: Limits = DEFAULT,
               worker: Callable[[str, Path, int], WorkerResult] = dispatch_worker, dry_run: bool = False,
               notify: Callable[[str, str], str] = _notify, recall: Callable[[str, int], list] = _recall,
               present: Callable[[dict], tuple] = _present, placeholder_tools: Optional[dict] = None) -> dict:
    """One full run of one folder. Always returns the run record (status: applied, noop, failed, skipped, dry-run)."""
    now = now or dt.datetime.now(ZoneInfo(limits.zone))
    run_id = new_run_id(now)
    record: dict = {"run_id": run_id, "slug": folder.slug, "name": folder.name, "started_at": now.isoformat(timespec="seconds"),
                    "status": "failed", "dry_run": dry_run}

    def finish(status: str, **extra: Any) -> dict:
        record.update(status=status, finished_at=dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), **extra)
        _write_json(_run_path(run_id), record)
        _log({"event": "run", **{k: record[k] for k in ("run_id", "name", "status") if k in record},
              "reason": record.get("reason", ""), "cost_usd": record.get("cost_usd", 0)})
        return record

    with _locked(folder.slug) as free:
        if not free:
            return finish("skipped", reason="another run of this folder is in progress")
        state = load_state(folder.slug)
        problem = preflight(folder.path, limits, ignore=folder.brief.ignore, **(placeholder_tools or {}))
        if problem:
            return finish("skipped", reason=problem)
        base, _ = read_folder(folder.path, folder.brief.ignore)
        record["base"] = {rel: _sha(data) for rel, data in base.items()}
        record["snapshot"] = str(snapshot(folder.slug, run_id, base, limits))
        run_number = state.get("runs_total", 0) + 1
        last = state.get("last_run_at")
        days_since = (now - dt.datetime.fromisoformat(last)).days if last else None
        try:
            work = stage(run_id, base, build_context(folder, recall))
            outcome = worker(build_prompt(folder, run_number, days_since, now.date(), _last_refused(folder.slug)), work, limits.run_timeout)
        except BackburnerError as error:
            return finish("failed", reason=str(error))
        record["cost_usd"] = round(outcome.cost_usd, 4)
        record["duration_sec"] = round(outcome.duration)
        record["staging"] = str(work)
        if not outcome.success:
            return _failed(folder, state, finish, f"worker failed (exit {outcome.exit_code}): {outcome.error}".strip())
        plan = validate(base, work, now.date(), limits, folder.brief.ignore, folder.brief.readonly)
        record["rejected"] = [{"rel": r.rel, "reason": r.reason} for r in plan.rejected]
        if plan.fatal:
            return _failed(folder, state, finish, plan.fatal)
        record["summary"] = plan.summary
        record["planned"] = [{"rel": c.rel, "kind": c.kind} for c in plan.changes]
        if dry_run:
            return finish("dry-run")

        def remember(applied: list[Applied]) -> None:
            record["applied"] = [dataclasses.asdict(a) for a in applied]
            record["status"] = "applying"
            _write_json(_run_path(run_id), record)

        remember([])
        applied, conflicts, error = apply_plan(folder, plan, base, run_id, remember)
        if error:
            return _failed(folder, state, finish, f"apply failed: {error}", applied=[])
        _index(folder.name, applied)
        owner_docs = [a for a in applied if a.role == "document" and not a.rel.startswith(f"{RESEARCHER}/")]
        progressed = bool(owner_docs)                      # the worker's own "improved" list is a claim, not a change
        stalled = 0 if progressed and not plan.summary["blocked_on_owner"] else state.get("stalled_runs", 0) + 1
        backing_off = stalled == limits.stall_after
        state.update(name=folder.name, runs_total=run_number, last_run_at=now.isoformat(timespec="seconds"), last_run_id=run_id,
                     stalled_runs=stalled, failures=0, last_status="applied" if applied else "noop",
                     signature=stat_signature(folder.path, folder.brief.ignore))
        save_state(folder.slug, state)
        status = "applied" if applied else "noop"
        finish(status, applied=[dataclasses.asdict(a) for a in applied], conflicts=conflicts)
        speaks = owner_docs or plan.summary["questions"] or conflicts or backing_off
        if speaks:
            record["notify_error"] = notify(render_note(folder, run_number, plan.summary, applied, conflicts, len(plan.rejected), backing_off),
                                            f"backburner:{folder.slug}:{run_id}")
            record["display_error"] = _show(folder, state, render_card(folder, run_number, plan.summary, applied, conflicts,
                                                                       len(plan.rejected), backing_off), present)
            _write_json(_run_path(run_id), record)
        return record


def _failed(folder: Folder, state: dict, finish: Callable[..., dict], reason: str, **extra: Any) -> dict:
    state.update(name=folder.name, failures=state.get("failures", 0) + 1, last_status="failed")
    save_state(folder.slug, state)
    return finish("failed", reason=reason, **extra)


# ───────────────────────────────────────────────────────────────────
# The nightly cycle
# ───────────────────────────────────────────────────────────────────
def _minutes(clock: str) -> int:
    hours, minutes = clock.split(":")
    return int(hours) * 60 + int(minutes)


def in_window(now: dt.datetime, limits: Limits = DEFAULT) -> bool:
    local = now.astimezone(ZoneInfo(limits.zone))
    return _minutes(limits.window_start) <= local.hour * 60 + local.minute < _minutes(limits.window_end)


def next_start(folder: Folder, now: dt.datetime, limits: Limits = DEFAULT) -> Optional[dt.datetime]:
    """When the nightly cycle will next take this folder, or None while it sits out (paused, done, stalled)."""
    if waiting_reason(folder, now, limits):
        return None
    zone = ZoneInfo(limits.zone)
    local = now.astimezone(zone)
    start = dt.datetime.combine(local.date(), dt.time(*divmod(_minutes(limits.window_start), 60)), zone)
    last = folder.state.get("last_run_at")
    if last and dt.datetime.fromisoformat(last).astimezone(zone).date() == local.date():
        return start + dt.timedelta(days=1)                  # tonight's run already happened
    if local < start:
        return start
    return local if in_window(now, limits) else start + dt.timedelta(days=1)


def _worker_model() -> str:
    """The model of the `backburner` route, so the panel shows what really runs; empty when dispatch is not importable."""
    try:
        from lib import dispatch
        return dispatch.ROUTES["backburner"]["model"]
    except (ImportError, KeyError, AttributeError):
        return ""


def overview(now: Optional[dt.datetime] = None, limits: Optional[Limits] = None) -> list[dict]:
    """One row per research folder for the owner panel: what the nightly cycle will do with it and how it went."""
    now = now or dt.datetime.now(dt.timezone.utc)
    limits = limits or effective_limits()
    latest = {}
    for record in runs_of_all():
        latest[record.get("slug")] = record                  # runs are listed oldest first
    rows, model = [], _worker_model()
    for folder in discover():
        state, record = folder.state, latest.get(folder.slug, {})
        following = next_start(folder, now, limits)
        rows.append({"name": folder.name, "slug": folder.slug, "status": folder.brief.status or "", "priority": folder.brief.priority,
                     "runs_total": state.get("runs_total", 0), "last_run_at": state.get("last_run_at"),
                     "last_status": state.get("last_status", ""), "failures": state.get("failures", 0),
                     "last_reason": record.get("reason", "") if record.get("status") in ("failed", "skipped") else "",
                     "waiting": waiting_reason(folder, now, limits), "next_run_at": following.isoformat(timespec="seconds") if following else None,
                     "window": f"{limits.window_start}-{limits.window_end}", "zone": limits.zone, "model": model})
    return rows


def recover_interrupted() -> list[str]:
    """A cycle that died while applying leaves a run in state `applying`: put back what it had applied."""
    healed = []
    for path in sorted((STATE / "runs").glob("*.json")) if (STATE / "runs").is_dir() else []:
        record = _read_json(path, {})
        if record.get("status") != "applying":
            continue
        folder_path = back_burner_root() / record["name"]
        folder = Folder(record["name"], folder_path, record["slug"], Brief(), {})
        base = load_snapshot(Path(record["snapshot"])) if Path(record.get("snapshot", "")).is_file() else {}
        applied = [Applied(**item) for item in record.get("applied", [])]
        problems = revert(folder, applied, base, record["run_id"]) if folder_path.is_dir() else ["folder is gone"]
        record.update(status="interrupted", recovered=not problems, problems=problems)
        _write_json(path, record)
        _log({"event": "recover", "run_id": record["run_id"], "problems": problems})
        healed.append(record["run_id"])
    return healed


def cycle(*, only: Optional[str] = None, dry_run: bool = False, limits: Limits = DEFAULT, ignore_window: bool = False,
          worker: Callable[[str, Path, int], WorkerResult] = dispatch_worker, clock: Callable[[], dt.datetime] = None,
          cycle_id: Optional[str] = None, **tools: Any) -> dict:
    """Run every due folder (or just `only`), one after another. Returns the cycle result."""
    clock = clock or (lambda: dt.datetime.now(ZoneInfo(limits.zone)))
    sweep_work()
    if cycle_id and not dry_run and not only:                         # a one-folder run does not carry the schedule
        _write_json(STATE / "schedule.json", {"window_start": limits.window_start, "window_end": limits.window_end,
                                              "max_runs": limits.max_runs, "cycle_id": cycle_id})
    result: dict = {"id": cycle_id, "started_at": clock().isoformat(timespec="seconds"), "recovered": recover_interrupted(),
                    "runs": [], "skipped": []}
    folders = discover()
    if only:
        folders = [find(only, folders)]
    started = 0
    for folder in folders:
        if not only:
            reason = waiting_reason(folder, clock(), limits)
            if not reason and started >= limits.max_runs:
                reason = f"the nightly cap of {limits.max_runs} runs is reached"
            if not reason and not ignore_window and not in_window(clock(), limits):
                reason = "outside the night window"
            if reason:
                result["skipped"].append({"name": folder.name, "reason": reason})
                continue
        record = run_folder(folder, now=clock(), limits=limits, worker=worker, dry_run=dry_run, **tools)
        started += 1
        result["runs"].append({"name": folder.name, "run_id": record["run_id"], "status": record["status"],
                               "reason": record.get("reason", "")})
    result["failed_folders"] = [f.name for f in folders if load_state(f.slug).get("failures", 0) >= 3]
    result["finished_at"] = clock().isoformat(timespec="seconds")
    if cycle_id:
        _write_json(STATE / "cycles" / cycle_id / "result.json", result)
    _log({"event": "cycle", "runs": len(result["runs"]), "skipped": len(result["skipped"])})
    return result


def start_cycle(cycle_id: str, *, only: Optional[str] = None, window_start: Any = None, window_end: Any = None,
                max_runs: Any = None, spawn: Callable[..., Any] = subprocess.Popen) -> dict:
    """Start `cycle` as a detached process of its own and record it, so butler-core can follow it to a result.

    The record is written before the process starts: a retry after a lost answer finds it and never starts a second
    cycle. The cycle's command line carries `-m lib.backburner cycle --id <id>`, which the core uses to make sure
    a process id still belongs to this job."""
    if not CYCLE_ID.fullmatch(cycle_id or ""):
        raise BackburnerError("invalid cycle id")
    limits_from(window_start, window_end, max_runs)                  # refuse unusable values before anything starts
    if only:
        find(only)                                                    # an unknown folder is refused here, not inside the job
    directory = STATE / "cycles" / cycle_id
    meta = directory / "meta.json"
    if meta.exists():
        return _read_json(meta, {})
    record = {"id": cycle_id, "monitor_pid": None, "started_at_ts": int(time.time()), "only": only}
    _write_json(meta, record)
    command = [sys.executable, "-m", "lib.backburner", "cycle", "--id", cycle_id]
    for flag, value in (("--only", only), ("--window-start", window_start), ("--window-end", window_end), ("--max-runs", max_runs)):
        if value is not None:
            command += [flag, str(value)]
    try:
        with (directory / "cycle.log").open("ab") as log:
            child = spawn(command, cwd=str(WORKSPACE), env={**os.environ, "PYTHONPATH": str(WORKSPACE)}, stdin=subprocess.DEVNULL,
                          stdout=log, stderr=log, start_new_session=True, close_fds=True)
    except OSError as error:
        meta.unlink(missing_ok=True)                                  # nothing started: a retry may try again
        raise BackburnerError(f"could not start the cycle: {error}") from None
    record["monitor_pid"] = child.pid
    _write_json(meta, record)
    return record


# ───────────────────────────────────────────────────────────────────
# Owner commands
# ───────────────────────────────────────────────────────────────────
BRIEF_TEMPLATE = """---
status: active
priority: 3
focus:
ignore: []
readonly: []
---

# {name}

## Цель
{goal}

## Ограничения
(бюджет, место, сроки, что нельзя)

## Что считать готовым
(как выглядит результат, по которому можно действовать)

## Предпочтения
(язык документов, формат, источники, которым доверяешь)
"""


def new_folder(name: str, goal: str) -> str:
    if not NAME_OK.match(name) or name.startswith("_"):
        raise BackburnerError(f"unusable folder name: {name!r}")
    existing = back_burner_root() / name
    if existing.exists():
        raise BackburnerError(f"{SUBDIR}/{name} already exists")
    vault.create(_vault_rel(name, BRIEF), BRIEF_TEMPLATE.format(name=name, goal=goal.strip() or "(опиши цель)"),
                 **_meta("cli"))
    return _vault_rel(name, BRIEF)


def set_status(token: str, status: str) -> str:
    """Change only the `status:` line of the brief (the one write to `_brief.md` that is allowed: the owner's own)."""
    folder = find(token)
    path = folder.path / BRIEF
    text = path.read_text()
    match = vault.FRONTMATTER_RE.match(text)
    if match and re.search(r"^status:.*$", match.group(1), flags=re.M):
        body = re.sub(r"^status:.*$", f"status: {status}", match.group(1), count=1, flags=re.M)
        updated = f"---\n{body}\n---\n" + text[match.end():]
    elif match:
        updated = f"---\nstatus: {status}\n{match.group(1)}\n---\n" + text[match.end():]
    else:
        updated = f"---\nstatus: {status}\n---\n\n{text}"
    if updated != text:
        vault.edit(_vault_rel(folder.name, BRIEF), updated, **_meta("cli"))
    return f"{folder.name}: {status}"


def runs_of_all() -> list[dict]:
    base = STATE / "runs"
    return [_read_json(path, {}) for path in sorted(base.glob("*.json"))] if base.is_dir() else []


def runs_of(slug: str) -> list[dict]:
    return [r for r in runs_of_all() if r.get("slug") == slug]


def undo(token: str, run_id: Optional[str] = None) -> dict:
    """Undo one run (the latest applied one by default). See revert for what is put back and what is left."""
    slug = None
    with contextlib.suppress(BackburnerError):
        slug = find(token).slug
    slug = slug or next((r["slug"] for r in reversed(runs_of_all()) if token in (r.get("name"), r.get("slug"))), None)
    if slug is None:
        raise BackburnerError(f"no folder or run history for {token!r}")
    candidates = [r for r in runs_of(slug) if r.get("status") == "applied" and (run_id is None or r["run_id"] == run_id)]
    if not candidates:
        raise BackburnerError("no applied run to undo")
    record = candidates[-1]
    with _locked(slug) as free:
        if not free:
            raise BackburnerError("a run of this folder is in progress")
        folder = Folder(record["name"], back_burner_root() / record["name"], slug, Brief(), {})
        base = load_snapshot(Path(record["snapshot"]))
        problems = revert(folder, [Applied(**item) for item in record["applied"]], base, record["run_id"])
        record.update(status="undone" if not problems else "undone-partly", undo_problems=problems)
        _write_json(_run_path(record["run_id"]), record)
        _log({"event": "undo", "run_id": record["run_id"], "problems": problems})
    return {"run_id": record["run_id"], "name": record["name"], "left_alone": problems}


# ───────────────────────────────────────────────────────────────────
# CLI
# ───────────────────────────────────────────────────────────────────
def _detach_stdin() -> None:
    """claude waits several seconds for stdin when it is open and silent; a cycle never has any."""
    os.dup2(os.open(os.devnull, os.O_RDONLY), 0)


def _describe(record: dict) -> str:
    extra = record.get("reason") or ""
    return f"{record['run_id']}  {record['status']:<10} {record.get('name', '')}  {extra}".rstrip()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="butler-backburner")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list").add_argument("--json", action="store_true")
    new = sub.add_parser("new")
    new.add_argument("name")
    new.add_argument("--goal", default="")
    for verb in ("pause", "resume", "done", "status", "history"):
        sub.add_parser(verb).add_argument("name")
    run = sub.add_parser("run")
    run.add_argument("name")
    run.add_argument("--dry-run", action="store_true")
    cyc = sub.add_parser("cycle")
    cyc.add_argument("--only")
    cyc.add_argument("--dry-run", action="store_true")
    cyc.add_argument("--id", dest="cycle_id")
    cyc.add_argument("--ignore-window", action="store_true")
    for parser_with_schedule in (cyc, sub.add_parser("start-cycle")):
        parser_with_schedule.add_argument("--window-start")
        parser_with_schedule.add_argument("--window-end")
        parser_with_schedule.add_argument("--max-runs", type=int)
    start = sub.choices["start-cycle"]
    start.add_argument("--id", dest="cycle_id", required=True)
    start.add_argument("--only")
    undo_parser = sub.add_parser("undo")
    undo_parser.add_argument("name")
    undo_parser.add_argument("--run", dest="run_id")
    args = parser.parse_args(argv)
    try:
        if args.command == "list" and args.json:
            print(json.dumps(overview(), ensure_ascii=False))
        elif args.command == "list":
            for folder in discover():
                state = folder.state
                print(f"{folder.name}\t{folder.brief.status or '?'}\tP{folder.brief.priority}\truns={state.get('runs_total', 0)}"
                      f"\tlast={state.get('last_run_at', '-')}\t{state.get('last_status', '-')}")
        elif args.command == "new":
            print(new_folder(args.name, args.goal))
        elif args.command in ("pause", "resume", "done"):
            print(set_status(args.name, {"pause": "paused", "resume": "active", "done": "done"}[args.command]))
        elif args.command == "run":
            _detach_stdin()
            result = cycle(only=args.name, dry_run=args.dry_run)
            for item in result["runs"]:
                print(_describe(item))
        elif args.command == "cycle":
            _detach_stdin()
            result = cycle(only=args.only, dry_run=args.dry_run, cycle_id=args.cycle_id, ignore_window=args.ignore_window,
                           limits=limits_from(args.window_start, args.window_end, args.max_runs))
            print(json.dumps(result, ensure_ascii=False))
        elif args.command == "start-cycle":
            print(json.dumps(start_cycle(args.cycle_id, only=args.only, window_start=args.window_start,
                                         window_end=args.window_end, max_runs=args.max_runs), ensure_ascii=False))
        elif args.command == "status":
            folder = find(args.name)
            print(json.dumps({"name": folder.name, "brief": dataclasses.asdict(folder.brief), "state": folder.state},
                             ensure_ascii=False, indent=1))
        elif args.command == "history":
            for record in runs_of(find(args.name).slug)[-10:]:
                print(_describe(record))
        elif args.command == "undo":
            print(json.dumps(undo(args.name, args.run_id), ensure_ascii=False))
    except BackburnerError as error:
        sys.stderr.write(f"{error}\n")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
