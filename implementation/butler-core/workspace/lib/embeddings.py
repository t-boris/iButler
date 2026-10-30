"""Embeddings — local semantic index over the vault (Phase 6 of memory).

Builds and queries a sqlite index of embedding vectors for vault notes,
using a local ollama model (embeddinggemma, 768-dim, multilingual RU/EN).

Storage: state/memory-index.db
    meta   — model / dim / version stamp; index is ignored on mismatch
    files  — path, mtime, size, chunk_count (for incremental reindex)
    chunks — path, scope, heading breadcrumb, line range, date, text, vector

Public API:
    is_available(timeout=1.0) -> bool      # ollama up AND model present
    reindex(full=False) -> ReindexStats    # mtime-based incremental
    search(query, k=20, min_sim=0.35) -> list[SemHit]

CLI:
    python3 -m lib.embeddings reindex [--full]
    python3 -m lib.embeddings search 'query' [--k N]
    python3 -m lib.embeddings status

Design notes:
    - Vectors are L2-normalized on write; search is a single numpy matrix
      product (brute force is exact and takes single-digit ms at ~20k chunks).
    - Everything degrades silently: lib.memory only calls this behind
      try/except, so keyword recall is unaffected when ollama is down or
      the index is missing/stale.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional

from lib import vault

OLLAMA_URL = "http://localhost:11434"
MODEL = "embeddinggemma"
DIM = 768
INDEX_VERSION = "1"

WORKSPACE = Path("/Users/boris/.openclaw/workspace-butler")
DB_PATH = WORKSPACE / "state" / "memory-index.db"


# BUTLER_OWNER_AI_SETTINGS — model/dimension stamp invalidates incompatible index.
_owner_ai_path = WORKSPACE / "config/ai-settings.json"
if _owner_ai_path.exists():
    try:
        if _owner_ai_path.is_symlink() or _owner_ai_path.stat().st_size > 65536:
            raise ValueError()
        _owner_ai = json.loads(_owner_ai_path.read_text())
        _owner_model = _owner_ai.get("embedding_model", MODEL)
        _owner_dim = _owner_ai.get("embedding_dimension", DIM)
        if not isinstance(_owner_model, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,150}", _owner_model) or "cloud" in _owner_model.lower():
            raise ValueError()
        if type(_owner_dim) is not int or not 1 <= _owner_dim <= 8192:
            raise ValueError()
        MODEL, DIM = _owner_model, _owner_dim
    except (OSError, ValueError, TypeError, AttributeError):
        raise RuntimeError("Invalid owner Butler semantic Settings") from None
OLLAMA_URL = "http://127.0.0.1:11434"
_owner_local_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
# END BUTLER_OWNER_AI_SETTINGS


MAX_FILE_BYTES = 32 * 1024 * 1024   # same cutoff as lib.memory keyword search
EMBED_BATCH = 32

# Chunking thresholds (characters)
SECTION_MAX = 1200   # sections longer than this get re-split into windows
WINDOW_SIZE = 1000
WINDOW_OVERLAP = 150
SECTION_MIN = 200    # sections shorter than this merge into the next sibling

# embeddinggemma asymmetric prompts — documents and queries are embedded
# with different prefixes; this measurably improves retrieval quality.
DOC_TEMPLATE = "title: {title} | text: {text}"
QUERY_TEMPLATE = "task: search result | query: {query}"

FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


# ───────────────────────────────────────────────────────────────────
# Ollama client
# ───────────────────────────────────────────────────────────────────
def is_available(timeout: float = 1.0) -> bool:
    """True when ollama answers AND the embedding model is pulled."""
    try:
        with _owner_local_opener.open(f"{OLLAMA_URL}/api/tags", timeout=timeout) as resp:
            data = json.loads(resp.read())
    except Exception:
        return False
    names = [m.get("name", "") for m in data.get("models", [])]
    return any(n == MODEL or n.startswith(MODEL + ":") for n in names)


def _embed_raw(inputs: list[str], timeout: float) -> list[list[float]]:
    body = json.dumps({"model": MODEL, "input": inputs}).encode()
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/embed", data=body,
        headers={"Content-Type": "application/json"},
    )
    with _owner_local_opener.open(req, timeout=timeout) as resp:
        data = json.loads(resp.read())
    vecs = data.get("embeddings")
    if not vecs or len(vecs) != len(inputs) or len(vecs[0]) != DIM:
        raise RuntimeError(f"unexpected embed response shape from {MODEL}")
    return vecs


def embed_docs(texts: list[str], timeout: float = 120.0) -> "np.ndarray":
    """Embed document chunks (batched), returns L2-normalized float32 array."""
    import numpy as np
    out = []
    for i in range(0, len(texts), EMBED_BATCH):
        from lib.knowledge_sources import wait_for_quiet
        wait_for_quiet()
        out.extend(_embed_raw(texts[i:i + EMBED_BATCH], timeout))
    arr = np.asarray(out, dtype=np.float32)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return arr / norms


def embed_query(query: str, timeout: float = 10.0) -> "np.ndarray":
    # 10s covers ollama cold start (model load after a restart); warm calls
    # return in ~50-150ms.
    import numpy as np
    vec = _embed_raw([QUERY_TEMPLATE.format(query=query)], timeout)[0]
    arr = np.asarray(vec, dtype=np.float32)
    n = float((arr ** 2).sum() ** 0.5) or 1.0
    return arr / n


# ───────────────────────────────────────────────────────────────────
# Chunking
# ───────────────────────────────────────────────────────────────────
@dataclass
class Chunk:
    heading: str          # "H1 > H2" breadcrumb ("" for preamble)
    start_line: int       # 1-based, inclusive
    end_line: int
    text: str


def _parse_frontmatter(content: str) -> tuple[dict, int]:
    """Return (fields, body_start_line). Cheap line parser, no yaml dep."""
    m = FRONTMATTER_RE.match(content)
    if not m:
        return {}, 1
    fields = {}
    for line in m.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t", "-")):
            key, _, val = line.partition(":")
            fields[key.strip()] = val.strip()
    return fields, m.group(0).count("\n") + 1


def _split_long(text: str, start_line: int) -> list[tuple[int, str]]:
    """Bound every model input, including a multi-megabyte single paragraph.

    Prefer a newline/word boundary in the latter half of each window. A long
    unbroken line is split by characters; no source text is silently omitted.
    """
    from bisect import bisect_left
    newlines = [i for i, char in enumerate(text) if char == "\n"]
    windows = []
    offset = 0
    while offset < len(text):
        end = min(offset + WINDOW_SIZE, len(text))
        if end < len(text):
            lower = offset + WINDOW_SIZE // 2
            boundary = text.rfind("\n", lower, end)
            if boundary < lower: boundary = text.rfind(" ", lower, end)
            if boundary >= lower: end = boundary + 1
        raw = text[offset:end]
        value = raw.strip()
        if value:
            first = offset + len(raw) - len(raw.lstrip())
            windows.append((start_line + bisect_left(newlines, first), value))
        if end == len(text): break
        offset = max(offset + 1, end - WINDOW_OVERLAP)
    return windows


def chunk_markdown(content: str) -> tuple[dict, list[Chunk]]:
    """Split note content into heading-based chunks.

    Returns (frontmatter_fields, chunks). Frontmatter is excluded from chunk
    text; short sections are merged forward; long sections are windowed.
    """
    fm, body_start = _parse_frontmatter(content)
    lines = content.splitlines()

    # Collect (level, title, start_line) heading markers
    sections: list[Chunk] = []
    crumb: dict[int, str] = {}
    sec_start = body_start
    sec_heading = ""
    buf: list[str] = []

    def flush(end_line: int):
        text = "\n".join(buf).strip()
        if text:
            sections.append(Chunk(sec_heading, sec_start, end_line, text))

    for i in range(body_start - 1, len(lines)):
        line = lines[i]
        m = re.match(r"^(#{1,3})\s+(.*)", line)
        if m:
            flush(i)
            level = len(m.group(1))
            crumb[level] = m.group(2).strip()
            for deeper in list(crumb):
                if deeper > level:
                    del crumb[deeper]
            sec_heading = " > ".join(crumb[l] for l in sorted(crumb))
            sec_start = i + 1
            buf = [line]
        else:
            buf.append(line)
    flush(len(lines))

    # Merge short sections forward, window long ones
    merged: list[Chunk] = []
    for sec in sections:
        if merged and len(merged[-1].text) < SECTION_MIN:
            prev = merged.pop()
            sec = Chunk(prev.heading or sec.heading, prev.start_line,
                        sec.end_line, prev.text + "\n\n" + sec.text)
        merged.append(sec)

    out: list[Chunk] = []
    for sec in merged:
        if len(sec.text) <= SECTION_MAX:
            out.append(sec)
        else:
            for w_start, w_text in _split_long(sec.text, sec.start_line):
                approx_end = w_start + w_text.count("\n")
                out.append(Chunk(sec.heading, w_start,
                                 min(approx_end, sec.end_line), w_text))
    return fm, out


# ───────────────────────────────────────────────────────────────────
# Index (sqlite)
# ───────────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta   (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS files  (path TEXT PRIMARY KEY,
                                   mtime REAL NOT NULL,
                                   size INTEGER NOT NULL,
                                   chunk_count INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS chunks (id INTEGER PRIMARY KEY,
                                   path TEXT NOT NULL,
                                   scope TEXT NOT NULL,
                                   heading TEXT,
                                   start_line INTEGER,
                                   end_line INTEGER,
                                   date TEXT,
                                   text TEXT NOT NULL,
                                   embedding BLOB NOT NULL);
CREATE INDEX IF NOT EXISTS idx_chunks_path ON chunks(path);
"""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    return con


def _meta(con: sqlite3.Connection) -> dict:
    return dict(con.execute("SELECT key, value FROM meta"))


def _meta_ok(con: sqlite3.Connection) -> bool:
    m = _meta(con)
    return (m.get("model") == MODEL and m.get("dim") == str(DIM)
            and m.get("version") == INDEX_VERSION)


def _stamp_meta(con: sqlite3.Connection) -> None:
    for k, v in (("model", MODEL), ("dim", str(DIM)),
                 ("version", INDEX_VERSION),
                 ("built_at", dt.datetime.now().isoformat(timespec="seconds"))):
        con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (k, v))


# Superseded/archived material stays readable in Obsidian but must not compete
# with live notes in semantic recall — nor make the dedup check match a report we
# already retired. (Boris 2026-08-03, research archive.)
EXCLUDED_PREFIXES = ("Research/Archive/",)


def is_excluded(rel_path: str) -> bool:
    from lib.knowledge_sources import eligible
    return not eligible(vault.vault_root() / rel_path, vault.vault_root())


def _index_targets() -> list[tuple[Path, str]]:
    """[(absolute_path, scope)] for all .md files we index."""
    from lib.knowledge_sources import files, authorized
    if not authorized(): raise RuntimeError("vault-source-unavailable")
    root = vault.vault_root()
    targets: list[tuple[Path, str]] = []
    for f in files(root):
        scope = "tasks" if f.relative_to(root).parts[0] == "Tasks" else "vault"
        targets.append((f, scope))
    return targets


def _note_date(fm: dict, rel_path: str) -> Optional[str]:
    for key in ("date", "captured_at", "created"):
        val = fm.get(key, "")
        m = DATE_RE.search(val)
        if m:
            return m.group(1)
    m = DATE_RE.search(rel_path)
    return m.group(1) if m else None


def _index_one(con, f: Path, rel: str, scope: str, st, *, commit: bool = True) -> Optional[int]:
    """Chunk + embed one file into the index, replacing whatever was there.
    Returns the chunk count, or None if the file could not be read."""
    try:
        content = f.read_text(errors="ignore")
    except Exception as exc:
        # Never silent: a file the indexer cannot read is a file recall and dedup
        # cannot see. 17 research reports from 2026-07-26..28 were missing from the
        # index for a week with nothing anywhere saying so (iCloud evicts vault files
        # to the cloud; reading one can fail or stall). Callers count these.
        print(f"[index] SKIPPED (unreadable): {rel}: {type(exc).__name__}: {exc}",
              file=sys.stderr)
        return None
    fm, chunks = chunk_markdown(content)
    date = _note_date(fm, rel)
    title = fm.get("title") or f.stem

    con.execute("DELETE FROM chunks WHERE path = ?", (rel,))
    if chunks:
        texts = [DOC_TEMPLATE.format(
                    title=f"{title[:300]} | {c.heading[:300]}" if c.heading else title[:300],
                    text=c.text)
                 for c in chunks]
        vecs = embed_docs(texts)
        con.executemany(
            "INSERT INTO chunks (path, scope, heading, start_line, end_line, date, text, embedding)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(rel, scope, c.heading, c.start_line, c.end_line, date,
              c.text, vecs[i].tobytes())
             for i, c in enumerate(chunks)])
    con.execute("INSERT OR REPLACE INTO files VALUES (?, ?, ?, ?)",
                (rel, st.st_mtime, st.st_size, len(chunks)))
    if commit:
        con.commit()
    return len(chunks)


def index_file(rel_path: str) -> int:
    """Index ONE vault file right now, outside the nightly reindex.

    Why this exists: `semantic-reindex-nightly` runs at 03:30 but the research
    drain runs at 14:00, so until now a report saved today was invisible to every
    lookup until the next night — which is exactly how five topics got researched
    twice on 2026-06-19. Calling this on save closes that window.

    Best-effort by design: if ollama is down we do NOT fail the caller (a report
    must still be saved); the nightly reindex picks the file up later. Returns the
    number of chunks indexed, or -1 if indexing was skipped.
    """
    if is_excluded(rel_path) or not is_available():
        return -1
    root = vault.vault_root()
    f = root / rel_path
    try:
        st = f.stat()
    except OSError:
        return -1
    if st.st_size > MAX_FILE_BYTES:
        return -1
    sub = rel_path.split("/", 1)[0]
    scope = "tasks" if sub == "Tasks" else "vault"
    con = _connect()
    try:
        if not _meta_ok(con):
            return -1          # index is stale/mismatched; let a full reindex fix it
        n = _index_one(con, f, rel_path, scope, st)
        return -1 if n is None else n
    finally:
        con.close()


def doc_vectors(prefix: str = "", *, exclude_archived: bool = True) -> tuple[list[str], "np.ndarray"]:
    """Report-level vectors: the L2-normalized mean of each file's chunk vectors.

    Chunk vectors answer "which passage is relevant"; averaging them gives a
    document vector that answers "is this the same topic", which is what dedup
    needs. Returns (paths, matrix) with matrix.shape == (len(paths), DIM); an
    empty matrix when nothing matches.
    """
    import numpy as np
    con = _connect()
    try:
        if prefix:
            rows = con.execute(
                "SELECT path, embedding FROM chunks WHERE path LIKE ? ORDER BY path",
                (prefix + "%",)).fetchall()
        else:
            rows = con.execute("SELECT path, embedding FROM chunks ORDER BY path").fetchall()
    finally:
        con.close()

    acc: dict[str, list] = {}
    for path, blob in rows:
        if exclude_archived and is_excluded(path):
            continue
        acc.setdefault(path, []).append(np.frombuffer(blob, dtype=np.float32))
    paths = sorted(acc)
    if not paths:
        return [], np.zeros((0, DIM), dtype=np.float32)
    mat = np.stack([np.mean(acc[p], axis=0) for p in paths])
    norms = np.linalg.norm(mat, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return paths, (mat / norms).astype(np.float32)


@dataclass
class ReindexStats:
    scanned: int = 0
    unchanged: int = 0
    reindexed: int = 0
    removed: int = 0
    chunks_added: int = 0
    skipped: int = 0        # unreadable/oversized — invisible to recall until fixed
    seconds: float = 0.0


def reindex(full: bool = False, progress: bool = False) -> ReindexStats:
    """Incrementally (re)build the index. `full` drops everything first.
    A model/dim/version mismatch forces a full rebuild automatically."""
    if not is_available():
        raise RuntimeError(f"ollama not reachable at {OLLAMA_URL} or model {MODEL!r} not pulled")
    t0 = time.time()
    con = _connect()
    try:
        if not _meta_ok(con):
            full = True

        # Look at the vault BEFORE touching the index. A full reindex wipes first and
        # refills second, so an unreadable vault would leave an empty index and take
        # recall, butler-map and research dedup down with it. The vault lives under
        # ~/Documents: macOS TCC denies it to a process without Full Disk Access, and
        # Path.rglob swallows the PermissionError and yields nothing — so "no files"
        # looks exactly like "empty vault". Refuse rather than guess.
        targets = _index_targets()
        known_count = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        if not targets and known_count:
            raise RuntimeError(
                f"vault appears empty ({vault.vault_root()}) but the index holds "
                f"{known_count} files — refusing to wipe it. Usually this means the "
                f"process cannot read the vault (macOS Full Disk Access), not that "
                f"the vault is really empty."
            )

        # Publish the whole rebuild as one SQLite transaction. Readers keep
        # seeing the previous index if embedding fails or the process dies.
        con.execute("BEGIN IMMEDIATE")
        if full:
            con.execute("DELETE FROM chunks")
            con.execute("DELETE FROM files")
            _stamp_meta(con)

        stats = ReindexStats()
        known = {p: (mt, sz) for p, mt, sz in
                 con.execute("SELECT path, mtime, size FROM files")}
        root = vault.vault_root()
        seen: set[str] = set()

        for f, scope in targets:
            rel = str(f.relative_to(root))
            seen.add(rel)
            stats.scanned += 1
            try:
                st = f.stat()
            except OSError as exc:
                print(f"[index] SKIPPED (stat failed): {rel}: {exc}", file=sys.stderr)
                stats.skipped += 1
                continue
            if st.st_size > MAX_FILE_BYTES:
                print(f"[index] SKIPPED (too big, {st.st_size}B): {rel}", file=sys.stderr)
                stats.skipped += 1
                continue
            if not full and rel in known and known[rel] == (st.st_mtime, st.st_size):
                stats.unchanged += 1
                continue

            n = _index_one(con, f, rel, scope, st, commit=False)
            if n is None:
                stats.skipped += 1
                continue
            stats.reindexed += 1
            stats.chunks_added += n
            if progress and stats.reindexed % 50 == 0:
                print(f"  ... {stats.reindexed} files, {stats.chunks_added} chunks", file=sys.stderr)

        if full and stats.skipped:
            raise RuntimeError(f"full reindex incomplete: {stats.skipped} files skipped; previous index preserved")

        # Drop files deleted from the vault
        for gone in set(known) - seen:
            con.execute("DELETE FROM chunks WHERE path = ?", (gone,))
            con.execute("DELETE FROM files WHERE path = ?", (gone,))
            stats.removed += 1
        _stamp_meta(con)
        con.commit()
        stats.seconds = round(time.time() - t0, 1)
        return stats
    finally:
        con.close()


# ───────────────────────────────────────────────────────────────────
# Search
# ───────────────────────────────────────────────────────────────────
@dataclass
class SemHit:
    path: str              # vault-relative
    scope: str             # "vault" | "tasks"
    heading: str
    start_line: int
    end_line: int
    date: Optional[str]
    sim: float             # cosine similarity 0..1
    snippet: str


def search(query: str, k: int = 20, min_sim: float = 0.35) -> list[SemHit]:
    """Semantic top-k over the index. Raises on any infrastructure problem —
    callers (lib.memory) treat exceptions as 'semantic unavailable'."""
    import numpy as np
    if not DB_PATH.exists():
        raise RuntimeError(f"index missing: {DB_PATH}")
    con = sqlite3.connect(DB_PATH)
    try:
        if not _meta_ok(con):
            raise RuntimeError("index meta mismatch — run butler-reindex --full")
        rows = con.execute(
            "SELECT path, scope, heading, start_line, end_line, date, text, embedding"
            " FROM chunks").fetchall()
        if not rows:
            return []
        mat = np.frombuffer(b"".join(r[7] for r in rows),
                            dtype=np.float32).reshape(len(rows), DIM)
        qv = embed_query(query)
        sims = mat @ qv
        order = np.argsort(-sims)[:max(k * 3, k)]
        out: list[SemHit] = []
        for idx in order:
            s = float(sims[idx])
            if s < min_sim:
                break
            p, scope, heading, sl, el, date, text, _ = rows[idx]
            snippet = " ".join(text.split())[:200]
            out.append(SemHit(p, scope, heading or "", sl or 0, el or 0,
                              date, s, snippet))
            if len(out) >= k:
                break
        return out
    finally:
        con.close()


def status() -> dict:
    if not DB_PATH.exists():
        return {"exists": False}
    con = sqlite3.connect(DB_PATH)
    try:
        files = con.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        chunks = con.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        return {"exists": True, "db": str(DB_PATH),
                "size_mb": round(DB_PATH.stat().st_size / 1e6, 1),
                "files": files, "chunks": chunks, **_meta(con)}
    finally:
        con.close()


# ───────────────────────────────────────────────────────────────────
# CLI
# ───────────────────────────────────────────────────────────────────
def _main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__, file=sys.stderr)
        return 2
    cmd = argv[0]
    if cmd == "reindex":
        full = "--full" in argv[1:]
        try:
            s = reindex(full=full, progress=True)
        except RuntimeError as e:
            sys.stderr.write(f"ERROR: {e}\n")
            return 1
        print(f"scanned={s.scanned} unchanged={s.unchanged} reindexed={s.reindexed} "
              f"removed={s.removed} skipped={s.skipped} chunks_added={s.chunks_added} "
              f"in {s.seconds}s")
        if s.skipped:
            print(f"WARNING: {s.skipped} file(s) could not be indexed — they are "
                  f"invisible to recall and to research dedup (see [index] lines above)",
                  file=sys.stderr)
        return 0
    elif cmd == "search":
        if len(argv) < 2:
            sys.stderr.write("usage: search 'query' [--k N]\n")
            return 2
        k = 20
        positional = []
        rest = argv[1:]
        while rest:
            a = rest.pop(0)
            if a == "--k":
                k = int(rest.pop(0))
            else:
                positional.append(a)
        try:
            hits = search(" ".join(positional), k=k)
        except RuntimeError as e:
            sys.stderr.write(f"ERROR: {e}\n")
            return 1
        if not hits:
            print("(no matches)")
            return 0
        for h in hits:
            print(f"[{h.scope:5}] {h.path}:{h.start_line}  ({h.date or '-'}  sim={h.sim:.2f})  {h.heading}")
            print(f"        {h.snippet}")
        return 0
    elif cmd == "status":
        print(json.dumps(status(), ensure_ascii=False, indent=2))
        return 0
    else:
        sys.stderr.write(f"unknown command: {cmd}\n")
        return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
