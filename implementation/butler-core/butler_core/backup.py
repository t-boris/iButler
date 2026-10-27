"""Backups to a separate physical disk (REQ-025, DEC-038, DEC-047, DEC-053).

Every 6 hours a new generation is written into an APFS sparse-bundle image on
the external disk: consistent SQLite copies plus the registered file sets.
Unchanged files are hard links to the previous generation. Each generation has
a SHA-256 manifest. Secrets never enter a backup: secret files are excluded,
secret values in configuration are replaced by references, and any other file
in which a credential pattern is found is left out and listed by path only.
Raw observation media, biometric references and regenerable bulk are excluded.
"""
from __future__ import annotations

import datetime as dt
import fnmatch
import hashlib
import json
import os
import plistlib
import shutil
import sqlite3
import subprocess
import tempfile
import time
from pathlib import Path

from .diagnostics import SECRET_KEY, SECRET_VALUE

HOME = Path.home()
INTERVAL = 6 * 3600
STALE = 12 * 3600
KEEP = 8
TEXT = {".json", ".jsonl", ".md", ".txt", ".yaml", ".yml", ".py", ".sh", ".zsh", ".toml", ".ini", ".cfg", ".env",
        ".plist", ".js", ".mjs", ".ts", ".log", ".csv", ".html", ".xml", ""}
SECRET_NAMES = ("*.session", "*.session-journal", "auth-profiles.json", "auth.json", "*token*", "*secret*",
                "*credential*", "*.pem", "*.key", "*.p12", ".env", "*.env", "cookies*", "*oauth*", "id_rsa*",
                "id_ed25519*", ".netrc", "*keychain*")
MEDIA = ("*.mp4", "*.mov", "*.mkv", "*.webm", "*.wav", "*.mp3", "*.m4a", "*.aac", "*.flac", "*.ogg", "*.opus",
         "*.avi", "*.heic")
COMMON_EXCLUDE = ("__pycache__", "node_modules", ".venv", "venv", "voice-env", ".git", ".pytest_cache", "build",
                  "dist", ".DS_Store", "tmp", ".cache", "*-wal", "*-shm", "*-journal")
SF_DATALESS = 0x40000000


class FileSet:
    def __init__(self, name, root, exclude=(), only=None, max_bytes=200 * 1024 * 1024, media=False,
                 skip_dataless=False, redact=()):
        self.name, self.root = name, Path(root)
        self.exclude = tuple(COMMON_EXCLUDE) + tuple(exclude)
        self.only, self.max_bytes, self.media = only, max_bytes, media
        self.skip_dataless, self.redact = skip_dataless, tuple(redact)

    def files(self):
        if self.root.is_file():
            yield self.root, Path(self.root.name)
            return
        if not self.root.is_dir():
            return
        for current, dirs, files in os.walk(self.root):
            here = Path(current)
            relative_dir = here.relative_to(self.root)
            dirs[:] = sorted(d for d in dirs if not self._excluded(relative_dir / d, d)
                             and not (here / d).is_symlink())
            for name in sorted(files):
                path = here / name
                relative = relative_dir / name
                if self._excluded(relative, name) or path.is_symlink():
                    continue
                if self.only and not any(fnmatch.fnmatch(str(relative), p) for p in self.only):
                    continue
                yield path, relative

    def _excluded(self, relative, name):
        text = str(relative)
        for pattern in self.exclude:
            if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(text, pattern) or text == pattern \
                    or text.startswith(pattern.rstrip("/") + "/"):
                return True
        return False


def default_sets(home=HOME):
    oc = home / ".openclaw"
    return [
        FileSet("workspace-butler", oc / "workspace-butler", exclude=("runs", "logs", "backups", "scratch", "work",
                                                                     "state/memory-index.db",
                                                                     "state/dispatches/*/output.log",
                                                                     "state/dispatches/*/stderr.log")),
        FileSet("workspace-bamberg-news", oc / "workspace-bamberg-news", exclude=("runs", "credentials")),
        FileSet("workspace-tetyana-dyck", oc / "workspace-tetyana-dyck", exclude=(".openclaw-cli-images", "runs",
                                                                                 "media", "repo", "repos")),
        FileSet("workspace-main", oc / "workspace"),
        FileSet("openclaw-cron", oc / "cron"),
        FileSet("openclaw-agents", oc / "agents", exclude=("*/agent",)),
        FileSet("openclaw-extensions", oc / "extensions"),
        FileSet("openclaw-config", oc / "openclaw.json", redact=("openclaw.json",)),
        FileSet("launch-agents", home / "Library/LaunchAgents", only=("com.boris.*.plist", "ai.openclaw.*.plist"),
                redact=("*.plist",)),
        FileSet("macstudio-butler", home / "Projects/MacStudioButler", exclude=("evidence/logs",)),
        FileSet("ambient-settings", home / ".local/share/butler-ambient",
                exclude=("raw", "images", "voice", "references", "preserved", "models", "backups", "spool",
                         "private-episodes", "ui-state.json", "logs", "*.sqlite3", "*.sqlite3-wal", "*.sqlite3-shm", "*.jpg", "*.png"), media=False),
        FileSet("vault", home / "Documents/KnowledgeDB", exclude=(".obsidian/cache", ".trash"), skip_dataless=True,
                max_bytes=50 * 1024 * 1024),
    ]


def default_databases(home=HOME, core_root=None):
    core_root = Path(core_root or home / ".local/share/butler-core")
    return {"butler-core/core.sqlite3": core_root / "core.sqlite3",
            "butler-core/conversation.sqlite3": core_root / "conversation.sqlite3",
            "butler-core/ingest.sqlite3": core_root / "ingest.sqlite3",
            "butler-ambient/ambient.sqlite3": home / ".local/share/butler-ambient/ambient.sqlite3"}


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def redact_value(value, path, source):
    if isinstance(value, dict):
        return {k: (({"secretRef": f"{source}:{path}.{k}".lstrip(".")} if isinstance(v, str) and v else v)
                    if SECRET_KEY.search(str(k)) and not isinstance(v, (dict, list)) else
                    redact_value(v, f"{path}.{k}", source)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_value(v, f"{path}[{i}]", source) for i, v in enumerate(value)]
    if isinstance(value, str) and SECRET_VALUE.search(value):
        return {"secretRef": f"{source}:{path}".lstrip(".")}
    return value


def redacted_bytes(path, relative):
    if path.suffix == ".plist":
        with open(path, "rb") as stream:
            data = plistlib.load(stream)
        return plistlib.dumps(redact_value(data, "", str(relative)), sort_keys=True)
    data = json.loads(path.read_text())
    return (json.dumps(redact_value(data, "", str(relative)), ensure_ascii=False, indent=2) + "\n").encode()


def is_sqlite(path):
    try:
        with open(path, "rb") as stream:
            return stream.read(16) == b"SQLite format 3\x00"
    except OSError:
        return False


def has_secret(path, binary=False):
    if (not binary and path.suffix.lower() not in TEXT) or path.stat().st_size > 200 * 1024 * 1024:
        return False
    try:
        with open(path, "rb") as stream:
            text = stream.read().decode("utf-8", errors="ignore")
    except OSError:
        return False
    return SECRET_VALUE.search(text) is not None


class Volume:
    """The sparse-bundle image on the external disk."""

    def __init__(self, disk=Path("/Volumes/Storage0"), folder="ButlerBackup", image="butler-core.sparsebundle",
                 mountpoint=None, size="800g", runner=subprocess.run):
        self.disk, self.folder = Path(disk), folder
        self.image = self.disk / folder / image
        self.mountpoint = Path(mountpoint or HOME / ".local/share/butler-core/backup-mount")
        self.size, self.runner = size, runner

    def available(self):
        return self.disk.is_dir() and os.path.ismount(self.disk)

    def _run(self, *command, timeout=600):
        result = self.runner(list(command), capture_output=True, text=True, timeout=timeout)
        if result.returncode:
            raise OSError(f"{command[0]} {command[1]} failed: {result.stderr.strip()[:200]}")
        return result

    def attach(self, readonly=False):
        if not self.image.exists():
            self.image.parent.mkdir(parents=True, exist_ok=True)
            self._run("/usr/bin/hdiutil", "create", "-type", "SPARSEBUNDLE", "-fs", "APFS", "-size", self.size,
                      "-volname", "ButlerBackup", str(self.image))
        self.mountpoint.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not os.path.ismount(self.mountpoint):
            command = ["/usr/bin/hdiutil", "attach", "-owners", "on", "-nobrowse", "-noautoopen",
                       "-mountpoint", str(self.mountpoint), str(self.image)]
            if readonly:
                command.insert(2, "-readonly")
            self._run(*command)
        return self.mountpoint

    def detach(self):
        if os.path.ismount(self.mountpoint):
            self._run("/usr/bin/hdiutil", "detach", str(self.mountpoint), timeout=120)


class Backup:
    def __init__(self, core, volume=None, sets=None, databases=None, keep=KEEP, interval=INTERVAL, clock=time.time,
                 version="0"):
        self.core, self.volume = core, volume or Volume()
        self.sets = sets if sets is not None else default_sets()
        self.databases = databases if databases is not None else default_databases(core_root=core.root)
        self.keep, self.interval, self.clock, self.version = max(4, keep), interval, clock, version
        self.running = False

    # -- scheduling ------------------------------------------------------------------

    def last(self):
        value = self.core.store.meta("backup_last")
        return None if value is None else float(value)

    def maybe_run(self):
        last = self.last()
        now = self.clock()
        if last is not None and now - last > STALE:
            self.core.diagnostics.raise_alarm("backup:stale", f"newest backup is {round((now - last) / 3600, 1)} h old")
        if last is None or now - last >= self.interval:
            return self.run()
        return None

    # -- writing -----------------------------------------------------------------------

    def run(self):
        if self.running:
            return {"ok": False, "reason": "backup already running"}
        self.running = True
        try:
            return self._run()
        finally:
            self.running = False

    def _run(self):
        started = time.monotonic()
        if not self.volume.available():
            self.core.diagnostics.raise_alarm("backup:disk-absent", "backup disk is not mounted")
            return {"ok": False, "reason": "backup disk is not mounted"}
        try:
            mount = self.volume.attach()
        except OSError as error:
            self.core.diagnostics.raise_alarm("backup:attach", str(error))
            return {"ok": False, "reason": str(error)}
        try:
            result = self.write_generation(mount)
        except OSError as error:
            self.core.diagnostics.raise_alarm("backup:write", f"{type(error).__name__}: {error}")
            return {"ok": False, "reason": f"{type(error).__name__}"}
        finally:
            try:
                self.volume.detach()
            except OSError:
                pass
        self.core.store.set_meta("backup_last", result["created_at"])
        self.core.store.set_meta("backup_generation", result["generation"])
        for key in ("backup:disk-absent", "backup:attach", "backup:write", "backup:stale"):
            self.core.diagnostics.clear_alarm(key)
        result["seconds"] = round(time.monotonic() - started, 1)
        self.core.diagnostics.record("backup.completed", {k: result[k] for k in ("generation", "files", "bytes",
                                                                                 "linked", "seconds")})
        return result

    def generations(self, mount):
        root = Path(mount) / "generations"
        if not root.is_dir():
            return []
        return sorted(p for p in root.iterdir() if p.is_dir() and not p.name.endswith(".partial")
                      and (p / "MANIFEST.json").is_file())

    def write_generation(self, mount):
        now = self.clock()
        name = dt.datetime.fromtimestamp(now, dt.timezone.utc).strftime("gen-%Y%m%dT%H%M%SZ")
        root = Path(mount) / "generations"
        root.mkdir(parents=True, exist_ok=True)
        previous = self.generations(mount)
        last = previous[-1] if previous else None
        last_manifest = json.loads((last / "MANIFEST.json").read_text())["files"] if last else {}
        target = root / (name + ".partial")
        if target.exists():
            shutil.rmtree(target)
        target.mkdir()
        files, excluded, skipped = {}, [], {"media": 0, "large": 0, "dataless": 0}
        total = linked = 0
        scratch = Path(tempfile.mkdtemp(prefix="backup-", dir=self.core.root))
        try:
            for relative, source in self.databases.items():
                if not Path(source).is_file():
                    continue
                copy = scratch / relative.replace("/", "__")
                self._sqlite_copy(source, copy)
                files[f"databases/{relative}"] = self._place(copy, target / "databases" / relative)
                total += files[f"databases/{relative}"]["size"]
            sizes = self._file_sets(target, last, last_manifest, files, excluded, skipped, scratch)
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        total += sizes[0]
        linked = sizes[1]
        manifest = {"format": 1, "generation": name, "created_at": now, "core_version": self.version,
                    "files": files, "secrets_excluded": sorted(excluded), "skipped": skipped,
                    "sets": [s.name for s in self.sets], "databases": sorted(self.databases)}
        manifest_path = target / "MANIFEST.json"
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, sort_keys=True))
        self._fsync(manifest_path)
        final = root / name
        target.rename(final)
        self._prune(mount)
        return {"ok": True, "generation": name, "created_at": now, "files": len(files), "bytes": total,
                "linked": linked, "secrets_excluded": len(excluded), "skipped": skipped,
                "generations": len(self.generations(mount))}

    def _file_sets(self, target, last, last_manifest, files, excluded, skipped, scratch):
        total = linked = 0
        for fileset in self.sets:
            for path, relative in fileset.files():
                key = f"files/{fileset.name}/{relative}"
                try:
                    stat = path.lstat()
                except OSError:
                    continue
                if fileset.skip_dataless and stat.st_flags & SF_DATALESS:
                    skipped["dataless"] += 1
                    continue
                if not fileset.media and any(fnmatch.fnmatch(path.name.lower(), p) for p in MEDIA):
                    skipped["media"] += 1
                    continue
                if stat.st_size > fileset.max_bytes:
                    skipped["large"] += 1
                    continue
                if any(fnmatch.fnmatch(path.name.lower(), p) for p in SECRET_NAMES):
                    excluded.append(key)
                    continue
                destination = target / key
                if any(fnmatch.fnmatch(path.name, p) for p in fileset.redact):
                    try:
                        data = redacted_bytes(path, key)
                    except (ValueError, OSError, plistlib.InvalidFileException):
                        excluded.append(key)
                        continue
                    files[key] = self._write(data, destination, stat)
                    total += len(data)
                    continue
                if is_sqlite(path):
                    # A live database is copied consistently through SQLite, never byte by byte.
                    copy = scratch / f"set-{len(files)}.sqlite3"
                    try:
                        self._sqlite_copy(path, copy)
                    except sqlite3.Error:
                        skipped.setdefault("unreadable", 0)
                        skipped["unreadable"] += 1
                        continue
                    if has_secret(copy, binary=True):
                        excluded.append(key)
                    else:
                        files[key] = self._place(copy, destination, stat)
                        total += files[key]["size"]
                    copy.unlink()
                    continue
                if has_secret(path):
                    excluded.append(key)
                    continue
                previous_entry = last_manifest.get(key)
                if previous_entry and previous_entry["size"] == stat.st_size and \
                        previous_entry["mtime"] == int(stat.st_mtime) and (last / key).is_file():
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    os.link(last / key, destination)
                    files[key] = previous_entry
                    linked += 1
                    continue
                try:
                    files[key] = self._place(path, destination, stat)
                except OSError:
                    skipped.setdefault("unreadable", 0)
                    skipped["unreadable"] += 1
                    continue
                total += stat.st_size
        return total, linked

    def _sqlite_copy(self, source, copy):
        source_db = sqlite3.connect(f"file:{source}?mode=ro", uri=True, timeout=30)
        try:
            target = sqlite3.connect(copy)
            try:
                source_db.backup(target)
            finally:
                target.close()
        finally:
            source_db.close()

    @staticmethod
    def _fsync(path):
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _place(self, source, destination, stat=None):
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        stat = stat or Path(source).stat()
        os.utime(destination, (stat.st_atime, stat.st_mtime))
        return {"sha256": sha256(destination), "size": destination.stat().st_size, "mtime": int(stat.st_mtime),
                "mode": stat.st_mode & 0o777}

    def _write(self, data, destination, stat):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "mtime": int(stat.st_mtime),
                "mode": stat.st_mode & 0o777, "redacted": True}

    def _prune(self, mount):
        complete = self.generations(mount)
        for old in complete[:-self.keep]:
            shutil.rmtree(old)
        for partial in (Path(mount) / "generations").glob("*.partial"):
            shutil.rmtree(partial)

    # -- status and restore ---------------------------------------------------------------

    def status(self):
        last = self.last()
        now = self.clock()
        return {"last": last, "age_h": None if last is None else round((now - last) / 3600, 2),
                "generation": self.core.store.meta("backup_generation"), "disk": self.volume.available(),
                "interval_h": self.interval / 3600, "keep": self.keep, "running": self.running}

    def restore(self, root, generation=None, mount=None):
        """Verify a generation and copy it into an empty root; return timings and restored accounting."""
        started = time.monotonic()
        root = Path(root)
        if root.exists() and any(root.iterdir()):
            raise OSError("restore target must be empty")
        attached = mount is None
        mount = mount or self.volume.attach(readonly=True)
        try:
            generations = self.generations(mount)
            if not generations:
                raise OSError("no complete backup generation")
            chosen = next((g for g in generations if g.name == generation), None) if generation else generations[-1]
            if chosen is None:
                raise OSError("generation not found")
            manifest = json.loads((chosen / "MANIFEST.json").read_text())
            bad = []
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            for key, entry in manifest["files"].items():
                source = chosen / key
                if not source.is_file() or sha256(source) != entry["sha256"]:
                    bad.append(key)
                    continue
                destination = root / key
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
                os.chmod(destination, entry.get("mode", 0o600) or 0o600)
        finally:
            if attached:
                self.volume.detach()
        # A later owner deletion dominates an older backup. Reconcile its
        # manifests and remove restored rule text before returning memory.
        for fileset in self.sets:
            if fileset.name != "workspace-butler": continue
            relative = Path("state/conversation-rules/deletions.json")
            live = fileset.root / relative
            restored_workspace = root / "files/workspace-butler"
            restored_vault = root / "files/vault"
            if not live.exists() or not restored_vault.is_dir(): continue
            target = restored_workspace / relative
            deletions = json.loads(target.read_text()) if target.exists() else {}
            deletions.update(json.loads(live.read_text()))
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(deletions)); target.chmod(0o600)
            from .rules import RuleStore
            RuleStore(restored_vault, restored_workspace).publish()
        accounting = restored_accounting(root / "databases" / "butler-core" / "core.sqlite3")
        return {"generation": chosen.name, "created_at": manifest["created_at"], "files": len(manifest["files"]),
                "corrupt": bad, "seconds": round(time.monotonic() - started, 1), "accounting": accounting}


def restored_accounting(path):
    if not Path(path).is_file():
        return None
    db = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        tasks = dict(db.execute("SELECT state, COUNT(*) FROM tasks GROUP BY state").fetchall())
        operations = dict(db.execute("SELECT state, COUNT(*) FROM operations GROUP BY state").fetchall())
        newest = db.execute("SELECT MAX(created_at) FROM tasks").fetchone()[0]
    finally:
        db.close()
    return {"tasks": tasks, "operations": operations, "newest_task_at": newest}

