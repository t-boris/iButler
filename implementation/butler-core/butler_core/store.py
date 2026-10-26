"""Durable local storage for the core (REQ-022, REQ-025).

One writer connection per process, short ``BEGIN IMMEDIATE`` transactions and
full synchronisation: a commit returns only after the data reached the disk
(``synchronous=FULL`` plus ``fullfsync`` on macOS). Admission is rejected
explicitly when free space falls below the reserve or SQLite reports a full
disk, instead of acknowledging work that might be lost.
"""
from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import threading
import time
import uuid
from pathlib import Path

RESERVE_BYTES = 256 * 1024 * 1024


class StorageExhausted(OSError):
    """Durable storage cannot accept more data; nothing was acknowledged."""


MIGRATIONS = [
    # 1: initial schema
    """
    CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
    CREATE TABLE events(id TEXT PRIMARY KEY, dedup_key TEXT NOT NULL UNIQUE, source TEXT NOT NULL,
      kind TEXT NOT NULL, persona TEXT NOT NULL, conversation TEXT, principal TEXT, privacy TEXT NOT NULL,
      occurred_at REAL NOT NULL, received_at REAL NOT NULL, payload TEXT NOT NULL, refs TEXT NOT NULL,
      correlation TEXT NOT NULL, task TEXT);
    CREATE TABLE rejections(id TEXT PRIMARY KEY, at REAL NOT NULL, source TEXT NOT NULL,
      reason TEXT NOT NULL, field TEXT, dedup_key TEXT);
    CREATE TABLE tasks(id TEXT PRIMARY KEY, kind TEXT NOT NULL, goal TEXT NOT NULL, initiator TEXT NOT NULL,
      persona TEXT NOT NULL, state TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
      deadline REAL, config_version INTEGER NOT NULL, conversation TEXT, reason TEXT,
      correlation TEXT NOT NULL, origin_event TEXT, channel TEXT);
    CREATE INDEX tasks_state ON tasks(state);
    CREATE TABLE task_history(task TEXT NOT NULL, at REAL NOT NULL, state TEXT NOT NULL, reason TEXT);
    CREATE TABLE steps(id TEXT PRIMARY KEY, task TEXT NOT NULL REFERENCES tasks(id), idx INTEGER NOT NULL,
      executor TEXT NOT NULL, action TEXT NOT NULL, depends_on TEXT NOT NULL, attempts INTEGER NOT NULL,
      max_attempts INTEGER NOT NULL, state TEXT NOT NULL, input TEXT NOT NULL, result TEXT, cancel TEXT,
      operation TEXT, ready_at REAL, started_at REAL, finished_at REAL, timeout REAL NOT NULL,
      UNIQUE(task, idx));
    CREATE INDEX steps_state ON steps(state);
    CREATE TABLE operations(id TEXT PRIMARY KEY, kind TEXT NOT NULL, persona TEXT NOT NULL, principal TEXT,
      task TEXT, step TEXT, params TEXT NOT NULL, params_hash TEXT NOT NULL, target TEXT NOT NULL,
      idempotency_key TEXT NOT NULL UNIQUE, export TEXT, receipt TEXT, state TEXT NOT NULL,
      attempts INTEGER NOT NULL, max_attempts INTEGER NOT NULL, created_at REAL NOT NULL,
      updated_at REAL NOT NULL, dispatched_at REAL, finished_at REAL, reason TEXT, approval TEXT,
      correlation TEXT NOT NULL, source TEXT NOT NULL, result TEXT, not_before REAL);
    CREATE INDEX operations_state ON operations(state);
    CREATE TABLE operation_attempts(operation TEXT NOT NULL, n INTEGER NOT NULL, started_at REAL NOT NULL,
      finished_at REAL, outcome TEXT, detail TEXT, PRIMARY KEY(operation, n));
    CREATE TABLE reconciliations(id TEXT PRIMARY KEY, operation TEXT NOT NULL, at REAL NOT NULL,
      outcome TEXT NOT NULL, evidence TEXT NOT NULL);
    CREATE TABLE resolutions(id TEXT PRIMARY KEY, operation TEXT NOT NULL UNIQUE, at REAL NOT NULL,
      resolver TEXT NOT NULL, channel TEXT NOT NULL, outcome TEXT NOT NULL, evidence TEXT NOT NULL);
    CREATE TABLE policy_decisions(id TEXT PRIMARY KEY, persona TEXT NOT NULL, principal TEXT,
      boundary TEXT NOT NULL, resource TEXT NOT NULL, outcome TEXT NOT NULL, rules TEXT NOT NULL,
      scope TEXT NOT NULL, reason TEXT NOT NULL, at REAL NOT NULL, config_version INTEGER NOT NULL,
      operation TEXT, correlation TEXT);
    CREATE TABLE approvals(id TEXT PRIMARY KEY, operation TEXT NOT NULL, params_hash TEXT NOT NULL,
      approver TEXT NOT NULL, channel TEXT NOT NULL, granted_at REAL NOT NULL, expires_at REAL NOT NULL,
      used_at REAL, revoked_at REAL);
    CREATE TABLE deliveries(id TEXT PRIMARY KEY, recipient TEXT NOT NULL, channel TEXT NOT NULL,
      persona TEXT NOT NULL, kind TEXT NOT NULL, content TEXT NOT NULL, idempotency_key TEXT NOT NULL UNIQUE,
      task TEXT, operation TEXT, attempts INTEGER NOT NULL, state TEXT NOT NULL, receipt TEXT,
      created_at REAL NOT NULL, updated_at REAL NOT NULL, first_attempt_at REAL, gateway TEXT,
      day TEXT, reason TEXT, correlation TEXT NOT NULL, not_before REAL);
    CREATE INDEX deliveries_state ON deliveries(state);
    CREATE TABLE config_versions(version INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE, content TEXT NOT NULL,
      author TEXT NOT NULL, created_at REAL NOT NULL, activated_at REAL, previous INTEGER, hash TEXT NOT NULL);
    CREATE TABLE conversations(id TEXT PRIMARY KEY, persona TEXT NOT NULL, participants TEXT NOT NULL,
      scope TEXT NOT NULL, channels TEXT NOT NULL, created_at REAL NOT NULL);
    CREATE TABLE selections(id TEXT PRIMARY KEY, principal TEXT NOT NULL, channel TEXT NOT NULL,
      target_kind TEXT NOT NULL, target TEXT NOT NULL, at REAL NOT NULL, ended_at REAL);
    CREATE TABLE cron_runs(key TEXT PRIMARY KEY, job TEXT NOT NULL, run_at REAL NOT NULL, agent TEXT,
      status TEXT NOT NULL, duration_ms INTEGER, observed_at REAL NOT NULL, task TEXT);
    CREATE TABLE diagnostics(id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, correlation TEXT,
      kind TEXT NOT NULL, detail TEXT NOT NULL);
    CREATE INDEX diagnostics_correlation ON diagnostics(correlation);
    CREATE TABLE metrics(id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, name TEXT NOT NULL,
      value REAL NOT NULL, labels TEXT NOT NULL);
    CREATE INDEX metrics_name ON metrics(name, at);
    CREATE TABLE alarms(key TEXT PRIMARY KEY, raised_at REAL NOT NULL, detail TEXT NOT NULL, cleared_at REAL);
    CREATE TABLE shadow(id TEXT PRIMARY KEY, at REAL NOT NULL, event TEXT, predicted TEXT NOT NULL,
      actual TEXT, confidence REAL, model TEXT NOT NULL, latency_ms REAL);
    """,
    # 2: owner feedback events for System 3 (REQ-027)
    """
    CREATE TABLE feedback(id TEXT PRIMARY KEY, at REAL NOT NULL, persona TEXT NOT NULL, principal TEXT NOT NULL,
      kind TEXT NOT NULL, subject TEXT, correlation TEXT, context TEXT NOT NULL, detail TEXT NOT NULL,
      analysed_at REAL);
    CREATE INDEX feedback_at ON feedback(at);
    """,
    # 3: files sent with a delivery, staged privately and removed when the delivery ends (W3)
    """
    CREATE TABLE attachments(delivery TEXT PRIMARY KEY, path TEXT NOT NULL, name TEXT NOT NULL, mime TEXT NOT NULL,
      size INTEGER NOT NULL, sha256 TEXT NOT NULL);
    """,
    # 4: System 3 lessons (W11): what the owner's signals taught, with provenance and status
    """
    CREATE TABLE lessons(id TEXT PRIMARY KEY, persona TEXT NOT NULL, cause TEXT NOT NULL, signal TEXT NOT NULL,
      title TEXT NOT NULL, text TEXT NOT NULL, confidence REAL NOT NULL, status TEXT NOT NULL, created_at REAL NOT NULL,
      updated_at REAL NOT NULL, sources TEXT NOT NULL, violations INTEGER NOT NULL DEFAULT 0, last_check REAL,
      exported_id TEXT, asked_at REAL, answer TEXT, confirmed_at REAL);
    CREATE INDEX lessons_status ON lessons(status);
    """,
]


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def loads(text, default=None):
    return default if text is None else json.loads(text)


def new_id(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def _disk_full(error):
    return isinstance(error, sqlite3.OperationalError) and (
        "database or disk is full" in str(error) or getattr(error, "sqlite_errorcode", None) == 13)


class Store:
    """Single-writer SQLite store. ``transaction`` is the only write path."""

    def __init__(self, root, name="core.sqlite3", reserve=RESERVE_BYTES, clock=time.time):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.path = self.root / name
        self.reserve, self.clock = reserve, clock
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None, timeout=10)
        self.path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        for pragma in ("journal_mode=WAL", "synchronous=FULL", "fullfsync=ON", "checkpoint_fullfsync=ON",
                       "foreign_keys=ON", "busy_timeout=10000"):
            self.db.execute("PRAGMA " + pragma)
        self._migrate()
        for suffix in ("-wal", "-shm"):
            extra = self.path.with_name(self.path.name + suffix)
            if extra.exists():
                extra.chmod(0o600)

    def _migrate(self):
        with self.lock:
            current = self.db.execute("PRAGMA user_version").fetchone()[0]
            if current > len(MIGRATIONS):
                raise RuntimeError(f"store schema {current} is newer than this core supports ({len(MIGRATIONS)})")
            for number in range(current, len(MIGRATIONS)):
                self.db.execute("BEGIN IMMEDIATE")
                try:
                    for statement in MIGRATIONS[number].split(";"):
                        if statement.strip():
                            self.db.execute(statement)
                    self.db.execute(f"PRAGMA user_version={number + 1}")
                    self.db.execute("COMMIT")
                except BaseException:
                    self.db.execute("ROLLBACK")
                    raise
            if self.meta("instance") is None:
                with self.transaction() as db:
                    db.execute("INSERT INTO meta VALUES('instance',?)", (uuid.uuid4().hex,))

    @property
    def schema_version(self):
        return self.db.execute("PRAGMA user_version").fetchone()[0]

    def free_bytes(self):
        stat = os.statvfs(self.root)
        return stat.f_bavail * stat.f_frsize

    def guard(self):
        if self.free_bytes() < self.reserve:
            raise StorageExhausted("free disk space below the core reserve")

    @contextlib.contextmanager
    def transaction(self, guard=True):
        """Atomic write. ``guard`` rejects new admissions when space is low; state
        transitions of already accepted work pass ``guard=False`` so they can finish."""
        with self.lock:
            if guard:
                self.guard()
            try:
                self.db.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError as error:
                if _disk_full(error):
                    raise StorageExhausted("database or disk is full") from None
                raise
            try:
                yield self.db
                self.db.execute("COMMIT")
            except BaseException as error:
                try:
                    self.db.execute("ROLLBACK")
                except sqlite3.OperationalError:
                    pass
                if _disk_full(error):
                    raise StorageExhausted("database or disk is full") from None
                raise

    def query(self, sql, params=()):
        with self.lock:
            return self.db.execute(sql, params).fetchall()

    def one(self, sql, params=()):
        with self.lock:
            return self.db.execute(sql, params).fetchone()

    def meta(self, key, default=None):
        row = self.one("SELECT value FROM meta WHERE key=?", (key,))
        return default if row is None else row[0]

    def set_meta(self, key, value, guard=False):
        with self.transaction(guard=guard) as db:
            db.execute("INSERT INTO meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (key, str(value)))

    def backup_to(self, target):
        """Consistent online copy of the database (SQLite backup API)."""
        target = Path(target)
        with self.lock:
            destination = sqlite3.connect(target)
            try:
                self.db.backup(destination)
            finally:
                destination.close()
        target.chmod(0o600)

    def close(self):
        with self.lock:
            self.db.close()
