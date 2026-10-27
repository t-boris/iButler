"""DEC-071 canonical Markdown rules; atomic, recoverable profile publication.

No permissions or executable settings are learned here. Profiles are advisory
context. The caller must authenticate an owner before accepting a mutation.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path


def digest(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def atomic(path, value, mode=0o600):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_file() and path.read_text() == value:
        path.chmod(mode)
        return
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".publish-")
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def json_text(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"


def content_hash(row):
    return digest(json_text({k: row[k] for k in ("title", "text", "kind", "scope", "status")}))


class Conflict(ValueError):
    """A concurrent edit is preserved and must be reconciled by the owner."""


class RuleStore:
    def __init__(self, vault, workspace, *, clock=time.time):
        self.vault, self.workspace, self.clock = Path(vault), Path(workspace), clock
        self.root = self.vault / "Butler/Rules"
        self.state = self.workspace / "state/conversation-rules"
        self.profile_path = self.state / "profile.json"
        self.deletions = self.state / "deletions.json"

    @contextlib.contextmanager
    def locked(self):
        if not self.vault.is_dir():
            raise OSError("vault-unavailable")
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.state / "write.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def tombstones(self):
        return json.loads(self.deletions.read_text()) if self.deletions.exists() else {}

    @staticmethod
    def identifier(value):
        if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,99}", value):
            raise ValueError("invalid rule identifier")
        return value

    @staticmethod
    def decode(path):
        text = path.read_text()
        if not text.startswith("---\n"):
            raise ValueError("rule needs metadata")
        _, header, body = text.split("---", 2)
        meta = json.loads(header)
        RuleStore.identifier(meta["id"])
        if meta.get("status") not in {"active", "candidate", "retired"}:
            raise ValueError("invalid rule status")
        if meta.get("kind") not in {"preference", "lesson", "fact"}:
            raise ValueError("invalid rule kind")
        scope = meta.get("scope")
        if (not isinstance(scope, dict) or set(scope) - {"mode", "application", "contains"}
                or scope.get("mode", "private") not in {"private", "general", "all"}):
            raise ValueError("invalid advisory rule scope")
        if type(meta.get("revision")) is not int or meta["revision"] < 1:
            raise ValueError("invalid rule revision")
        body = body.strip()
        if not 1 <= len(body) <= 8000:
            raise ValueError("invalid rule text")
        row = {**meta, "text": body, "path": str(path)}
        return {**row, "hash": content_hash(row)}

    def scan(self):
        deleted = self.tombstones()
        if deleted:
            import shutil
            for identifier in deleted:
                self.identifier(identifier)
                shutil.rmtree(self.root / ".history" / identifier, ignore_errors=True)
            backup = self.state / "migration/LESSONS.before.md"
            if backup.exists():
                text = backup.read_text()
                for identifier in deleted:
                    text = re.sub(r"(?ms)^## " + re.escape(identifier) + r"\s+—.*?(?=^## |\Z)", "", text)
                atomic(backup, text, 0o400)
        rows = {}
        for path in sorted(self.root.glob("*.md")):
            if path.is_symlink(): raise ValueError("symlink rule is forbidden")
            row = self.decode(path)
            if row["id"] in deleted:
                path.unlink()  # deletion manifests apply before restored memory is served
                continue
            if row["id"] in rows: raise Conflict("duplicate canonical identity")
            rows[row["id"]] = row
        return rows

    def _write(self, row):
        meta = {k: v for k, v in row.items() if k not in {"text", "hash", "path"}}
        content = "---\n" + json_text(meta) + "---\n\n" + row["text"].strip() + "\n"
        path = Path(row.get("path") or self.root / (row["id"] + ".md"))
        history = self.root / ".history" / row["id"] / (str(row["revision"]) + ".md")
        if history.exists() and history.read_text() != content:
            raise Conflict("immutable revision already exists")
        atomic(path, content)
        # Immutable revision history is subordinate to the canonical file.
        atomic(history, content, 0o400)
        return path

    def _publish(self):
        rows = self.scan()
        known_path = self.state / "known.json"
        old = json.loads(known_path.read_text()) if known_path.exists() else {r["id"]: r for r in self.profile().get("rules", [])}
        for row in rows.values():
            prior = old.get(row["id"])
            if prior and prior["hash"] != row["hash"] and row["revision"] <= prior["revision"]:
                row["revision"] = prior["revision"] + 1
                row["updated_at"] = self.clock()
            history = self.root / ".history" / row["id"]
            # A crash may leave canonical metadata ahead of known.json. Never
            # replace that immutable history, including after a manual edit.
            existing = history / (str(row["revision"]) + ".md")
            if existing.exists() and self.decode(existing)["hash"] != row["hash"]:
                row["revision"] = max([row["revision"], *[int(p.stem) for p in history.glob("*.md") if p.stem.isdigit()]]) + 1
                row["updated_at"] = self.clock()
            self._write(row)
        atomic(known_path, json_text({r["id"]: {k: r[k] for k in ("revision", "hash")} for r in rows.values()}))
        active = [{k: v for k, v in r.items() if k != "path"}
                  for r in sorted(rows.values(), key=lambda r: r["id"]) if r["status"] == "active"]
        index = [{"id": r["id"], "revision": r["revision"], "hash": r["hash"]} for r in active]
        version = digest(json_text(active))
        profile = {"schema": 1, "version": version, "rules": active, "index": index}
        # Index and compiled profile are the same atomic generation.
        atomic(self.profile_path, json_text(profile))
        body = "# Lessons — generated read-only from vault Butler/Rules\n\n"
        body += "\n\n".join("## " + r["id"] + " — " + r["title"] + "\n\n" + r["text"] for r in active)
        atomic(self.workspace / "LESSONS.md", body + "\n", 0o444)
        return profile

    def publish(self):
        with self.locked(): return self._publish()

    def profile(self):
        if not self.profile_path.exists(): return {"schema": 1, "version": "empty", "rules": [], "index": []}
        value = json.loads(self.profile_path.read_text())
        if value.get("schema") != 1 or not isinstance(value.get("rules"), list):
            raise ValueError("incompatible profile")
        return value

    def block_context(self, reason):
        """Remove derived context when its source grant is revoked/unavailable."""
        value = {"schema": 1, "version": "source-unavailable", "rules": [], "index": [], "reason": reason}
        atomic(self.profile_path, json_text(value))
        atomic(self.workspace / "LESSONS.md", "# Lessons — generated read-only\n\nSource authorization unavailable.\n", 0o444)
        return value

    def put(self, identifier, title, text, *, operation, authorized=False, source="owner", kind="preference",
            scope=None, status="active", expected_hash=None, publish=True):
        if not authorized or source not in {"owner", "manual", "confirmed-system3", "migration"}:
            raise PermissionError("owner authorization required for standing rules")
        identifier = self.identifier(identifier)
        if identifier in self.tombstones(): raise Conflict("deleted rule cannot be revived")
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 8000: raise ValueError("invalid text")
        scope = scope or {"mode": "private"}
        if (not isinstance(title, str) or not title.strip() or not isinstance(operation, str) or not operation
                or kind not in {"preference", "lesson", "fact"} or status not in {"active", "candidate", "retired"}
                or not isinstance(scope, dict) or set(scope) - {"mode", "application", "contains"}
                or scope.get("mode", "private") not in {"private", "general", "all"}
                or any(not isinstance(v, str) or len(v) > 300 for v in scope.values())):
            raise ValueError("invalid rule metadata")
        request_hash = digest(json_text([identifier, title, text.strip(), kind, scope, status]))
        with self.locked():
            receipt_path = self.state / "receipts" / (digest(operation) + ".json")
            if receipt_path.exists():
                receipt = json.loads(receipt_path.read_text())
                if receipt["request_hash"] != request_hash: raise Conflict("operation identity reused with different content")
                if receipt.get("applied"): return receipt
            rows = self.scan()
            old = rows.get(identifier)
            if old and old.get("operation") == operation:
                row = old  # recover crash between file write and receipt publication
                if old["request_hash"] != request_hash: raise Conflict("interrupted operation differs")
                self._write(row)
            else:
                if old and expected_hash != old["hash"]: raise Conflict("concurrent edit preserved")
                row = {"id": identifier, "title": str(title)[:200], "text": text.strip(), "kind": kind,
                       "scope": scope, "status": status, "source": source, "revision": old["revision"] + 1 if old else 1,
                       "created_at": old["created_at"] if old else self.clock(), "updated_at": self.clock(),
                       "operation": operation, "request_hash": request_hash, **({"path": old["path"]} if old else {})}
                path = self._write(row)
                self.decode(path)  # validate before any publication
            receipt = {"id": identifier, "revision": row["revision"], "content_hash": content_hash(row),
                       "operation": operation, "request_hash": request_hash, "received": True, "saved": True,
                       "applied": False, "verified": False, "written_at": self.clock()}
            atomic(receipt_path, json_text(receipt))
            if not publish: return receipt
            profile = self._publish()
            receipt.update(applied=True, profile=profile["version"])
            atomic(receipt_path, json_text(receipt))
            return receipt

    def change(self, identifier, *, operation, authorized=False, text=None, scope=None, status=None, delete=False):
        if not authorized: raise PermissionError("owner authorization required")
        identifier = self.identifier(identifier)
        if delete:
            with self.locked():
                manifest = self.tombstones()
                manifest[identifier] = {"at": self.clock(), "operation_hash": digest(operation)}
                atomic(self.deletions, json_text(manifest))
                rows = self.scan()
                # scan already removes restored/deleted canonical sources.
                import shutil
                shutil.rmtree(self.root / ".history" / identifier, ignore_errors=True)
                for receipt in (self.state / "receipts").glob("*.json"):
                    if json.loads(receipt.read_text()).get("id") == identifier: receipt.unlink()
                profile = self._publish()
                return {"id": identifier, "deleted": True, "profile": profile["version"],
                        "removed": ["canonical", "history", "profile", "index", "write-receipts", "legacy-view"]}
        row = self.scan()[identifier]
        return self.put(identifier, row["title"], row["text"] if text is None else text, operation=operation,
                        authorized=True, source="owner", kind=row["kind"], scope=scope or row["scope"],
                        status=status or row["status"], expected_hash=row["hash"])

    def migrate(self):
        legacy = self.workspace / "LESSONS.md"
        backup = self.state / "migration/LESSONS.before.md"
        done = self.state / "migration/done.json"
        if done.exists(): return 0
        if backup.exists(): original = backup.read_text()
        else:
            if not legacy.exists() or "generated read-only" in legacy.read_text()[:100]: return 0
            original = legacy.read_text()
            atomic(backup, original, 0o400)
        blocks = re.split(r"(?m)^## (?=L-)", original)[1:]
        count = 0
        for block in blocks:
            heading, _, body = block.partition("\n")
            match = re.match(r"(L-\d{4}-\d{2}-\d{2}-\d+)\s+—\s+(.+)", heading)
            if not match: continue
            identifier, title = match.groups()
            if identifier in self.tombstones(): continue
            self.put(identifier, title, body.strip() or title, operation="migration:" + identifier,
                     authorized=True, source="migration", kind="lesson", publish=False)
            count += 1
        profile = self.publish()
        for path in (self.state / "receipts").glob("*.json"):
            receipt = json.loads(path.read_text())
            if receipt["operation"].startswith("migration:"):
                receipt.update(applied=True, profile=profile["version"])
                atomic(path, json_text(receipt))
        atomic(done, json_text({"count": count, "original_hash": digest(original), "profile": profile["version"]}))
        return count

    def matching(self, *, mode="private", application="", speech=""):
        # A manual edit is published before the next matching decision. Invalid
        # input leaves the last compatible profile active and reports the failure.
        error = None
        try: profile = self.publish()
        except (OSError, ValueError) as failure:
            profile, error = self.profile(), type(failure).__name__
        selected, excluded = [], []
        deleted = self.tombstones()
        for rule in profile["rules"]:
            scope = rule["scope"]
            applies = (rule["id"] not in deleted and scope.get("mode", "private") in {mode, "all"}
                       and (not scope.get("application") or scope["application"] == application)
                       and (not scope.get("contains") or scope["contains"].casefold() in speech.casefold()))
            (selected if applies else excluded).append(rule)
        return {"version": profile["version"], "selected": selected,
                "excluded": [{"id": r["id"], "revision": r["revision"], "reason": "scope"} for r in excluded], "error": error}
