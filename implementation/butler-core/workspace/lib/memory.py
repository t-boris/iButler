"""Memory — remember + recall across vault, sessions, and tasks.

Three storage planes, queried together:
    1. Vault    — canonical long-term, Markdown files in /Users/boris/Documents/KnowledgeDB
    2. Sessions — OpenClaw jsonl logs at ~/.openclaw/agents/butler/sessions/*.jsonl
    3. Tasks    — Tasks/{Inbox,Active,Waiting,Done}.md (also in vault but indexed separately)

Public API:
    remember(text, *, kind, source="boris-said", project=None, confidence="high") -> str
    recall(query, *, k=5, scope=("vault","sessions","tasks")) -> list[Hit]

`remember` writes to Knowledge/<kind>/<YYYY-MM-DD>-<slug>.md via vault adapter.
`recall` is hybrid (Phase 6): ripgrep keyword search + local-embedding semantic
search over state/memory-index.db (lib.embeddings, ollama/embeddinggemma).
Semantic channel degrades silently to keyword-only when ollama or the index
is unavailable. Disable explicitly with BUTLER_NO_SEMANTIC=1 or --no-semantic.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable, Literal, Optional

from lib import vault

KNOWLEDGE_ROOT = "Knowledge"
SESSIONS_DIR = Path("/Users/boris/.openclaw/agents/butler/sessions")

# Butler-relevant vault subfolders (default search roots).
# UIUC-CS/, UIUC-iMBA/, attachments/, course materials, etc. are excluded by
# default because they're huge and rarely relevant to the butler's job.
# Override with --scope all-vault to search everything (slow on large vaults).
DEFAULT_VAULT_ROOTS = [
    "Daily Notes",
    "Tasks",
    "Knowledge",
    "Goals",
    "Projects",
    "Research",
    "News",
    "People",
    "Companies",
]

# Skip files larger than this — almost never useful for recall, slow to read.
MAX_FILE_BYTES = 32 * 1024 * 1024

KnowledgeKind = Literal[
    "fact", "preference", "decision", "event", "person", "claim", "project-fact", "rule"
]

VALID_KINDS = {"fact", "preference", "decision", "event", "person", "claim", "project-fact", "rule"}


# ───────────────────────────────────────────────────────────────────
# Slugify
# ───────────────────────────────────────────────────────────────────
def _slugify(text: str, max_len: int = 60) -> str:
    s = re.sub(r"[^\w\s-]", "", text.lower())
    s = re.sub(r"[-\s]+", "-", s).strip("-")
    return s[:max_len] or "untitled"


# ───────────────────────────────────────────────────────────────────
# remember()
# ───────────────────────────────────────────────────────────────────
def remember(
    text: str,
    *,
    kind: str = "fact",
    source: str = "boris-said",
    project: Optional[str] = None,
    confidence: str = "high",
) -> str:
    """Write a memory note to Knowledge/<kind>/YYYY-MM-DD-<slug>.md.

    Returns the vault-relative path.
    Raises ValueError on unknown kind.
    """
    if kind not in VALID_KINDS:
        raise ValueError(f"kind must be one of {sorted(VALID_KINDS)}; got {kind!r}")

    today = dt.date.today().isoformat()
    slug = _slugify(text)
    rel = f"{KNOWLEDGE_ROOT}/{kind}/{today}-{slug}.md"

    body = f"""---
type: memory
kind: {kind}
source: {source}
captured_at: {today}
{"project: " + project if project else ""}
confidence: {confidence}
---

# {text}

<!-- Add context, reasoning, or related links below. -->
"""
    # Trim empty project line if no project
    body = re.sub(r"\n\n", "\n", body, count=1) if not project else body
    # vault.create with `from` meta so the write.yaml rule for from:boris-said triggers
    res = vault.create(rel, body, **{"from": source if source in ("research", "news") else "boris-said"})
    if not res.ok:
        raise RuntimeError(f"vault refused to create {rel}: action={res.action} audit={res.audit_id}")
    return rel


# ───────────────────────────────────────────────────────────────────
# butler-core path (W6): a recall is recorded as a `knowledge.recall` operation
# before any source is read, so the persona's source scope is checked at the
# search boundary. Nothing leaves the machine; only scopes and a query checksum.
# ───────────────────────────────────────────────────────────────────
class RecallRefused(PermissionError):
    pass


def _core_recall_recorded(scope, query: str, k: int) -> None:
    try:
        from lib import butler_core_client as core
    except ImportError:
        return
    if not core.switched("knowledge"):
        return
    import hashlib, uuid
    digest = hashlib.sha256(query.encode()).hexdigest()
    principal = os.environ.get("BUTLER_CORE_PRINCIPAL", "agent:butler")
    try:
        op = core.Client().execute("knowledge.recall", "butler", "knowledge:recall",
                                   {"scopes": list(scope), "query_sha256": digest, "k": k},
                                   f"recall:{uuid.uuid4().hex[:12]}", principal=principal, timeout=30)
    except core.CoreUnavailable:
        raise RecallRefused("butler-core недоступно — поиск по памяти не выполнен") from None
    except core.CoreError as error:
        raise RecallRefused(f"butler-core отказало: {error.code}: {error.message}") from None
    if op.get("state") != "succeeded":
        raise RecallRefused(f"поиск отклонён: {op.get('reason') or op.get('state')}")


# ───────────────────────────────────────────────────────────────────
# recall()
# ───────────────────────────────────────────────────────────────────
@dataclass
class Hit:
    scope: str             # "vault" | "sessions" | "tasks"
    path: str              # vault-relative for vault/tasks; absolute for sessions
    line_no: int           # 1-based; 0 if not line-level
    snippet: str           # short context around match
    date: Optional[str]    # YYYY-MM-DD if extractable, else None
    score: float
    provenance: str = ""   # where the evidence comes from and whether it is a confirmed fact or a model inference


# ───────────────────────────────────────────────────────────────────
# Provenance (REQ-023): every hit says what kind of evidence it is.
# ───────────────────────────────────────────────────────────────────
CANONICAL_SOURCES = {"boris-said", "owner", "todoist", "boris"}
INFERRED_SOURCES = {"research", "news", "classifier", "model", "ambient", "ambient-observation", "inbox-classifier"}


def _frontmatter(path: Path, limit: int = 2048) -> dict:
    try:
        head = path.read_text(errors="replace")[:limit]
    except OSError:
        return {}
    if not head.startswith("---"):
        return {}
    end = head.find("\n---", 3)
    if end < 0:
        return {}
    fields = {}
    for line in head[3:end].splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip().strip('"')
    return fields


def provenance_of(hit: "Hit", vault_root: Optional[Path] = None, *, semantic: bool = False) -> str:
    """A short label: evidence kind, its source and whether it is canonical or inferred."""
    channel = " · semantic match" if semantic else ""
    if hit.scope == "sessions":
        return "session transcript · model output, unverified" + channel
    if hit.scope == "tasks":
        return "task mirror · canonical (Todoist)" + channel
    root = vault_root or vault.vault_root()
    fields = _frontmatter(root / hit.path)
    source = fields.get("source") or fields.get("from") or ""
    kind = fields.get("type") or ""
    if fields.get("managed_by") == "ai-butler" or kind in ("memory", "research-queue", "research", "digest", "news"):
        if source in CANONICAL_SOURCES:
            standing = "canonical (owner said)"
        elif source in INFERRED_SOURCES or kind in ("research", "news", "digest"):
            standing = "model inference, unverified"
        else:
            standing = "butler note, " + (f"confidence={fields['confidence']}" if fields.get("confidence") else "unverified")
        label = f"{kind or 'note'}" + (f" · source={source}" if source else "") + f" · {standing}"
    else:
        label = "vault note · human-authored, canonical"
    return label + channel


def _recency_weight(date_str: Optional[str], today: Optional[dt.date] = None) -> float:
    """More recent = higher. Range roughly 0.1 .. 1.0."""
    if not date_str:
        return 0.3
    today = today or dt.date.today()
    try:
        d = dt.date.fromisoformat(date_str)
    except ValueError:
        return 0.3
    age_days = max(0, (today - d).days)
    if age_days == 0:
        return 1.0
    # Half-life ~30 days
    return max(0.1, 0.5 ** (age_days / 30.0))


def _extract_date_from_path(path: str) -> Optional[str]:
    m = re.search(r"(\d{4}-\d{2}-\d{2})", path)
    return m.group(1) if m else None


def _rg_bin() -> Optional[str]:
    """Absolute path to ripgrep, or None. Checks known install locations
    explicitly because cron sessions run with a minimal PATH."""
    p = shutil.which("rg")
    if p:
        return p
    for cand in ("/opt/homebrew/bin/rg", "/usr/local/bin/rg"):
        if Path(cand).is_file():
            return cand
    return None


def _search_files(query: str, files: Iterable[Path], scope: str,
                  root: Optional[Path] = None, max_per_file: int = 1) -> list[Hit]:
    out: list[Hit] = []
    q_lower = query.lower()
    for f in files:
        try:
            if root is not None:
                from lib.knowledge_sources import eligible
                if not eligible(f, root): continue
            if f.stat().st_size > MAX_FILE_BYTES:
                continue  # skip giant files (course notes, generated content)
            content = f.read_text(errors="ignore")
        except Exception:
            continue
        path_str = str(f.relative_to(root)) if root else str(f)
        hits_in_file = 0
        date = _extract_date_from_path(path_str)
        for i, line in enumerate(content.splitlines(), start=1):
            if q_lower in line.lower():
                snippet = line.strip()[:200]
                base = 1.0 if q_lower in line.lower().split() else 0.5
                title_bonus = 0.5 if line.startswith("# ") or line.startswith("## ") else 0
                score = (base + title_bonus) * _recency_weight(date)
                out.append(Hit(scope=scope, path=path_str, line_no=i,
                               snippet=snippet, date=date, score=score))
                hits_in_file += 1
                if hits_in_file >= max_per_file:
                    break
    return out


def _search_with_ripgrep(query: str, search_roots: list[Path], scope: str,
                         vault_root_path: Path, max_per_file: int = 1,
                         timeout_sec: int = 5) -> list[Hit]:
    """Use ripgrep for fast vault search. Returns Hits."""
    out: list[Hit] = []
    rg = _rg_bin()
    if not search_roots or not rg:
        return out
    # Filter existing paths only
    from lib.knowledge_sources import files, eligible
    existing = [str(p) for root in search_roots if root.exists()
                for p in ([root] if root.is_file() else files(root)) if eligible(p, vault_root_path)]
    if not existing:
        return out
    if len(existing) > 400:
        for start in range(0, len(existing), 400):
            out.extend(_search_with_ripgrep(query, [Path(p) for p in existing[start:start+400]], scope,
                                           vault_root_path, max_per_file, timeout_sec))
        return out
    try:
        proc = subprocess.run(
            # -g '*.md': recall only cares about notes; without it rg also
            # reads attachments (images/PDFs), and evicted iCloud files can
            # block reads for seconds and blow the timeout.
            [rg, "--json", "--no-heading", "-m", str(max_per_file),
             "--max-filesize", "32M", "-g", "*.md", "-i", "--", query, *existing],
            capture_output=True, text=True, timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        return out
    if proc.returncode not in (0, 1):  # 0=hits, 1=no hits
        return out
    for line in proc.stdout.splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("type") != "match":
            continue
        data = e.get("data", {})
        path = data.get("path", {}).get("text", "")
        lines = data.get("lines", {}).get("text", "")
        line_no = data.get("line_number", 0)
        try:
            rel = str(Path(path).relative_to(vault_root_path))
        except ValueError:
            rel = path
        date = _extract_date_from_path(rel)
        snippet = lines.strip()[:200]
        base = 1.0 if query.lower() in snippet.lower().split() else 0.6
        title_bonus = 0.5 if snippet.startswith(("# ", "## ")) else 0
        score = (base + title_bonus) * _recency_weight(date)
        out.append(Hit(scope=scope, path=rel, line_no=line_no, snippet=snippet,
                       date=date, score=score))
    return out


def _search_sessions(query: str, max_per_file: int = 2) -> list[Hit]:
    """Search OpenClaw session jsonl files. Each line is a json event;
    we grep within message text fields.
    """
    out: list[Hit] = []
    if not SESSIONS_DIR.exists():
        return out
    q_lower = query.lower()
    for f in sorted(SESSIONS_DIR.glob("*.jsonl"), reverse=True)[:200]:  # newest first, cap files
        date = _extract_date_from_path(f.name)
        hits_in_file = 0
        try:
            with open(f) as fh:
                for i, line in enumerate(fh, start=1):
                    if q_lower not in line.lower():
                        continue
                    try:
                        e = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    # Try to extract a human-readable text snippet
                    text = (e.get("text") or e.get("message") or
                            e.get("content") or json.dumps(e))[:200]
                    if q_lower not in text.lower():
                        continue
                    if not date:
                        ts = e.get("ts") or e.get("timestamp") or ""
                        date = ts[:10] if isinstance(ts, str) else None
                    score = 0.6 * _recency_weight(date)
                    out.append(Hit(scope="sessions", path=str(f), line_no=i,
                                   snippet=text.strip(), date=date, score=score))
                    hits_in_file += 1
                    if hits_in_file >= max_per_file:
                        break
        except Exception:
            continue
    return out


def _semantic_hits(query: str, scope: tuple[str, ...]) -> list[Hit]:
    """Semantic channel (Phase 6). Raises on any infrastructure problem —
    recall() treats exceptions as 'semantic unavailable' and moves on.

    Score maps onto the keyword scale: (0.5 + cos_sim) * recency, so a strong
    semantic hit competes with a whole-word keyword hit without dominating
    title matches.
    """
    from lib import embeddings
    if not embeddings.is_available(timeout=1.0):
        raise RuntimeError("ollama unavailable")
    want = {s for s in ("vault", "tasks") if s in scope or "semantic" in scope}
    if "all-vault" in scope:
        want.add("vault")
    out: list[Hit] = []
    for s in embeddings.search(query, k=20, min_sim=0.35):
        if s.scope not in want:
            continue
        from lib.knowledge_sources import current_fragment
        if not current_fragment(vault.vault_root(), s.path, s.snippet): continue
        snippet = (f"[{s.heading}] " if s.heading else "") + s.snippet
        score = (0.5 + max(0.0, s.sim)) * _recency_weight(s.date)
        out.append(Hit(scope=s.scope, path=s.path, line_no=s.start_line,
                       snippet=snippet[:200], date=s.date, score=score))
    for hit in out:
        hit._semantic = True      # embedding match: reported as such in the provenance
    return out


def _merge_semantic(keyword: list[Hit], semantic: list[Hit]) -> list[Hit]:
    """Dedupe semantic hits to the best chunk per file, then merge with
    keyword hits: when both channels agree on a file the pair collapses into
    one Hit with a single +0.15 agreement bonus; semantic-only hits are
    appended as-is."""
    best: dict[tuple[str, str], Hit] = {}
    for sem in semantic:
        key = (sem.path, sem.scope)
        if key not in best or sem.score > best[key].score:
            best[key] = sem
    merged = list(keyword)
    kw_keys = {(kw.path, kw.scope): kw for kw in keyword}
    for key, sem in best.items():
        kw = kw_keys.get(key)
        if kw is not None:
            kw.score = max(kw.score, sem.score) + 0.15
        else:
            merged.append(sem)
    return merged


def recall(query: str, *, k: int = 5,
           scope: tuple[str, ...] = ("vault", "sessions", "tasks"),
           semantic: bool = True) -> list[Hit]:
    """Return top-K hits across the requested scopes, sorted by score desc.

    Vault search uses ripgrep (fast) restricted to DEFAULT_VAULT_ROOTS subfolders.
    Pass scope=("all-vault",) to search the entire vault (slow).
    Pass scope=("semantic",) for embedding search only.
    The semantic channel runs automatically alongside vault/tasks keyword
    search; it is skipped when semantic=False, BUTLER_NO_SEMANTIC=1, or the
    ollama/index infrastructure is unavailable.
    """
    _core_recall_recorded(scope, query, k)
    hits: list[Hit] = []
    vault_root = vault.vault_root()
    semantic_only = scope == ("semantic",) or set(scope) == {"semantic"}

    if not semantic_only and ("vault" in scope or "all-vault" in scope):
        if "all-vault" in scope:
            roots = [vault_root]
        else:
            roots = [vault_root / sub for sub in DEFAULT_VAULT_ROOTS]
            # Always exclude Tasks/ from vault scope — covered by tasks scope
            roots = [r for r in roots if r.name != "Tasks"]
        # Prefer ripgrep when available
        if _rg_bin():
            hits.extend(_search_with_ripgrep(query, roots, "vault", vault_root))
        else:
            for r in roots:
                if not r.exists():
                    continue
                files = r.rglob("*.md")
                hits.extend(_search_files(query, files, "vault", root=vault_root))

    if not semantic_only and "tasks" in scope:
        tasks_dir = vault_root / "Tasks"
        if tasks_dir.exists():
            if _rg_bin():
                hits.extend(_search_with_ripgrep(query, [tasks_dir], "tasks", vault_root))
            else:
                hits.extend(_search_files(query, tasks_dir.glob("*.md"), "tasks", root=vault_root))

    if not semantic_only and "sessions" in scope:
        hits.extend(_search_sessions(query))

    # Semantic channel (Phase 6) — never allowed to break keyword recall.
    if semantic and os.environ.get("BUTLER_NO_SEMANTIC") != "1" and (
            semantic_only or "vault" in scope or "all-vault" in scope
            or "tasks" in scope):
        try:
            hits = _merge_semantic(hits, _semantic_hits(query, scope))
        except Exception:
            pass

    # Old semantic chunks cannot reintroduce deleted, excluded or unloaded files.
    from lib.knowledge_sources import eligible
    hits = [h for h in hits if h.scope == "sessions" or eligible(vault_root / h.path, vault_root)]
    hits.sort(key=lambda h: h.score, reverse=True)
    chosen = hits[:k]
    for hit in chosen:
        if not hit.provenance:
            hit.provenance = provenance_of(hit, vault_root, semantic=getattr(hit, "_semantic", False))
    return chosen


# ───────────────────────────────────────────────────────────────────
# CLI
# ───────────────────────────────────────────────────────────────────
def _main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__, file=sys.stderr)
        print("\nUsage:", file=sys.stderr)
        print("  python3 -m lib.memory remember 'text' [--kind K] [--source S] [--project P] [--confidence C]", file=sys.stderr)
        print("  python3 -m lib.memory recall 'query' [--k 5] [--scope vault,sessions,tasks,semantic] [--no-semantic]", file=sys.stderr)
        return 2
    cmd = argv[0]
    if cmd == "remember":
        kwargs = {}
        positional = []
        rest = argv[1:]
        while rest:
            a = rest.pop(0)
            if a.startswith("--"):
                key = a.lstrip("-")
                if not rest:
                    sys.stderr.write(f"flag missing value: {a}\n"); return 2
                kwargs[key] = rest.pop(0)
            else:
                positional.append(a)
        if not positional:
            sys.stderr.write("usage: remember 'text' [opts]\n"); return 2
        text = " ".join(positional)
        try:
            path = remember(text, **kwargs)
        except (ValueError, RuntimeError) as e:
            sys.stderr.write(f"ERROR: {e}\n"); return 1
        print(path)
        return 0
    elif cmd == "recall":
        if len(argv) < 2:
            sys.stderr.write("usage: recall 'query' [--k N] [--scope vault,sessions,tasks]\n"); return 2
        k = 5
        scope: tuple[str, ...] = ("vault", "sessions", "tasks")
        semantic = True
        positional = []
        rest = argv[1:]
        while rest:
            a = rest.pop(0)
            if a == "--k":
                k = int(rest.pop(0))
            elif a == "--scope":
                scope = tuple(s.strip() for s in rest.pop(0).split(","))
            elif a == "--no-semantic":
                semantic = False
            else:
                positional.append(a)
        query = " ".join(positional)
        hits = recall(query, k=k, scope=scope, semantic=semantic)
        if not hits:
            print("(no matches)")
            return 0
        for h in hits:
            print(f"[{h.scope:8}] {h.path}:{h.line_no}  ({h.date or '-'}  score={h.score:.2f})")
            print(f"           {h.snippet}")
            print(f"           prov: {h.provenance}")
        return 0
    else:
        sys.stderr.write(f"unknown command: {cmd}\n")
        return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
