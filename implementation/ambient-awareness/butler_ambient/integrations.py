"""Narrow, reversible owner Settings adapters for the existing Butler workspace."""
from __future__ import annotations

import ast
import hashlib
import os
from pathlib import Path


LEGACY_DISPATCH = '''
# BUTLER_OWNER_AI_SETTINGS — local Settings; existing cost guards remain below.
def _owner_ai_settings():
    path = WORKSPACE / "config/ai-settings.json"
    if not path.exists():
        return
    try:
        if path.is_symlink() or path.stat().st_size > 65536:
            raise ValueError()
        data = json.loads(path.read_text())
        approved = {(v["backend"], v["model"]) for v in ROUTES.values()}
        routes = data.get("routes", {})
        for kind, value in routes.items():
            if kind not in ROUTES or set(value) != {"backend", "model"} or (value["backend"], value["model"]) not in approved:
                raise ValueError()
        executables = data.get("executables", {})
        for backend, binary in executables.items():
            if backend not in {"claude", "codex"} or not isinstance(binary, str) or Path(binary).name != backend or not shutil.which(binary):
                raise ValueError()
        for kind, value in routes.items():
            ROUTES[kind] = {**ROUTES[kind], **value}
        global CLAUDE_BIN, CODEX_BIN_CANDIDATES
        CLAUDE_BIN = executables.get("claude", CLAUDE_BIN)
        if "codex" in executables:
            CODEX_BIN_CANDIDATES = [executables["codex"]]
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise RuntimeError("Invalid owner Butler AI Settings; fix local configuration") from None

_owner_ai_settings()
# END BUTLER_OWNER_AI_SETTINGS

'''

DISPATCH = LEGACY_DISPATCH.replace(
    '        routes = data.get("routes", {})',
    r'''        catalog_path = WORKSPACE / "config/model-catalog.json"
        if catalog_path.exists():
            import time as _owner_time
            import re as _owner_re
            if catalog_path.is_symlink() or catalog_path.stat().st_size > 1000000:
                raise ValueError()
            catalog = json.loads(catalog_path.read_text())
            if catalog.get("version") != 1:
                raise ValueError()
            legacy_approved = approved
            approved = set()
            for backend, provider in catalog.get("providers", {}).items():
                if backend not in {"claude", "codex"}:
                    continue
                if not provider.get("fetched_at"):
                    approved.update(pair for pair in legacy_approved if pair[0] == backend)
                    continue
                if not 0 <= _owner_time.time() - provider.get("fetched_at", 0) <= 604800:
                    continue
                for model in provider.get("models", []):
                    for name in [model.get("id"), *model.get("aliases", [])]:
                        if isinstance(name, str) and _owner_re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/\[\]-]{0,149}", name):
                            approved.add((backend, name))
        routes = data.get("routes", {})''')

EMBEDDING = '''
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

'''


def adapted(text, role):
    """Fail on unexpected source structure instead of replacing unrelated work."""
    if "BUTLER_OWNER_AI_SETTINGS" in text:
        if role == "dispatch" and LEGACY_DISPATCH in text:
            value = text.replace(LEGACY_DISPATCH,DISPATCH,1)
            ast.parse(value)
            return value
        if role == "dispatch" and DISPATCH not in text:
            raise ValueError("Butler dispatch adapter changed; review before replacing it")
        return text
    marker = "DEFAULT_TIMEOUT = 1800\n" if role == "dispatch" else 'DB_PATH = WORKSPACE / "state" / "memory-index.db"\n'
    if text.count(marker) != 1:
        raise ValueError("Butler integration source changed; adapter needs review")
    value = text.replace(marker, marker + "\n" + (DISPATCH if role == "dispatch" else EMBEDDING), 1)
    if role == "embedding":
        value = value.replace("urllib.request.urlopen(", "_owner_local_opener.open(")
    ast.parse(value)
    return value


def install(workspace):
    """Back up exact originals privately. No task, CLI agent or gateway is run."""
    workspace = Path(workspace).resolve()
    backup = workspace / "state/owner-settings-backups"
    backup.mkdir(mode=0o700, parents=True, exist_ok=True)
    prepared = []
    for role in ("dispatch", "embedding"):
        path = workspace / "lib" / ("dispatch.py" if role == "dispatch" else "embeddings.py")
        if path.is_symlink() or not path.is_file():
            raise ValueError("Expected existing Butler integration file")
        original = path.read_bytes()
        updated = adapted(original.decode(), role).encode()
        before, after = (hashlib.sha256(data).hexdigest() for data in (original, updated))
        prepared.append((path, original, updated, before, after))
    rows = []
    for path, original, updated, before, after in prepared:
        if original != updated:
            saved = backup / (path.name + "." + before)
            if not saved.exists():
                with saved.open("xb") as stream:
                    os.chmod(saved, 0o600)
                    stream.write(original)
            temporary = path.with_suffix(".settings.tmp")
            temporary.write_bytes(updated)
            temporary.chmod(path.stat().st_mode & 0o777)
            # Do not clobber a concurrent edit to another Butler feature.
            if path.read_bytes() != original:
                temporary.unlink()
                raise ValueError("Butler integration changed during installation")
            temporary.replace(path)
        rows.append({"file": str(path), "before_sha256": before, "after_sha256": after,
                     "changed": original != updated, "backup": str(backup / (path.name + "." + before)) if original != updated else None})
    return rows
