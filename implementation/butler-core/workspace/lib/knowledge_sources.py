"""The shared keyword/semantic vault boundary, including unloaded iCloud files."""
import os
import re
from pathlib import Path


# Generated, dependency and cache folders are never notes. 2026-11-02: 575 of 2660 indexed files were node_modules
# documentation. Only unambiguous tooling names: a folder an owner may name freely (Build, Dist, target) stays in.
GENERATED_DIRECTORIES = frozenset({"node_modules", ".git", ".venv", "venv", "__pycache__", ".next", ".nuxt", ".cache",
                                   ".pytest_cache", ".mypy_cache", ".tox", "site-packages", "bower_components"})


def excluded_directory(name):
    return (name.casefold() in {".trash", ".obsidian", ".history"} | GENERATED_DIRECTORIES
            or re.fullmatch(r"(?:\d+\s*[-_]\s*)?archives?", name, re.I) is not None)


def authorized():
    try: from lib import butler_core_client as core
    except ImportError: return True  # isolated library fixtures / before core installation
    try:
        content = core.Client().call("config.active", _socket_timeout=.2).get("content") or {}
        sources = set(content.get("personas", {}).get("butler", {}).get("sources", []))
        sources.update(g["resource"] for g in content.get("grants", []) if g.get("persona") == "butler" and g.get("kind") == "source")
        return "vault" in sources
    except Exception: return False


# The ambient service answers a status request in about 1 s (up to 1.5 s measured) while it analyses on a loaded
# machine. A timeout shorter than its normal latency turns every poll into a "miss" and fails the nightly reindex.
STATUS_TIMEOUT = 4.0


def wait_for_quiet():
    """Cooperate with live hearing between embedding batches, never inspect text."""
    import json
    import socket
    import time
    endpoint = Path.home() / ".local/share/butler-ambient/ambient.sock"
    if not endpoint.exists(): return
    misses = 0  # a few genuine misses in a row (no answer within STATUS_TIMEOUT) are weather, not an outage
    for _ in range(120):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(STATUS_TIMEOUT); client.connect(str(endpoint))
                client.sendall(b'{"command":"status"}\n')
                result = json.loads(client.makefile("rb").readline())
            result = result.get("result", result)
            if result.get("enabled") is False: raise RuntimeError("background-analysis-paused")
            if result.get("analysis_busy") is not True: return
            misses = 0
        except (OSError, ValueError):
            misses += 1
            if misses >= 5: raise RuntimeError("background-analysis-status-unavailable") from None
        time.sleep(.5)
    raise RuntimeError("background-analysis-deferred-for-speech")


def eligible(path, root, keep_evicted=False):
    """keep_evicted: count a note that macOS evicted to iCloud (UF_DATALESS) as still in the vault. Reading it would download it,
    so indexing never does, but pruning must not treat "not downloaded right now" as "deleted"."""
    path, root = Path(path), Path(root)
    try:
        relative = path.relative_to(root)
        if any(excluded_directory(part) for part in relative.parts): return False
        if path.name.casefold() == "secrets.md" or path.is_symlink(): return False
        if not path.resolve().is_relative_to(root.resolve()): return False
        stat = path.stat()
        if getattr(stat, "st_flags", 0) & 0x40000000:  # UF_DATALESS: do not download
            return keep_evicted and path.suffix.casefold() == ".md"
        if relative.parts[:2] == ("Butler", "Rules") and path.suffix.casefold() == ".md":
            import json
            try:
                header = path.read_text().split("---", 2)[1]
                if json.loads(header).get("status") != "active": return False
            except (ValueError, IndexError): return False
        return path.suffix.casefold() == ".md"
    except OSError: return False


def files(root):
    root = Path(root)
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not excluded_directory(d)
                   and not (Path(directory) / d).is_symlink()]
        for name in sorted(names):
            path = Path(directory) / name
            if eligible(path, root): yield path


def current_fragment(root, relative, snippet):
    path = Path(root) / relative
    if not eligible(path, root): return False
    try: return bool(snippet) and " ".join(snippet.split()) in " ".join(path.read_text(errors="ignore").split())
    except OSError: return False
