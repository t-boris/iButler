"""Private local storage. Expiration never depends on successful analysis."""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import threading
import tempfile
import time
import uuid
import errno
from pathlib import Path

from .event_frames import DAILY_LIMIT, WINDOW_SECONDS


class AnalysisBufferFull(OSError):
    pass


class Store:
    SPOOL_LIMIT = 512 * 1024 * 1024          # default temporary-media budget; config "spool_limit_mb" overrides it
    SPOOL_LIMIT_RANGE_MB = (64, 32768)

    def __init__(self, root: Path, *, spool_limit_mb=None):
        if spool_limit_mb is not None:
            low, high = self.SPOOL_LIMIT_RANGE_MB
            if type(spool_limit_mb) is not int or not low <= spool_limit_mb <= high:
                raise ValueError(f"spool_limit_mb must be an integer between {low} and {high}")
            self.SPOOL_LIMIT = spool_limit_mb * 1024 * 1024
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        for folder in ("raw", "preserved", "references"):
            (self.root / folder).mkdir(exist_ok=True, mode=0o700)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "ambient.sqlite3", check_same_thread=False)
        (self.root / "ambient.sqlite3").chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
          PRAGMA journal_mode=WAL;
          PRAGMA secure_delete=ON;
          CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS agreements(id TEXT PRIMARY KEY, name TEXT NOT NULL,
            active INTEGER NOT NULL, reference TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS media(id TEXT PRIMARY KEY, source TEXT NOT NULL,
            start REAL NOT NULL, end REAL NOT NULL, path TEXT, preserved TEXT,
            analyzed INTEGER NOT NULL DEFAULT 0, storage_class TEXT NOT NULL DEFAULT 'requested',
            size_bytes INTEGER NOT NULL DEFAULT 0, complete INTEGER NOT NULL DEFAULT 1, samples TEXT);
          CREATE TABLE IF NOT EXISTS observations(id TEXT PRIMARY KEY, media_id TEXT NOT NULL,
            source TEXT NOT NULL, start REAL NOT NULL, end REAL NOT NULL, facts TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS observation_media(observation_id TEXT NOT NULL, media_id TEXT NOT NULL,
            PRIMARY KEY(observation_id,media_id));
          CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY, start REAL NOT NULL,
            end REAL, source TEXT NOT NULL, reason TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS identities(id TEXT PRIMARY KEY, name TEXT NOT NULL,
            description TEXT NOT NULL, kind TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS corrections(subject TEXT PRIMARY KEY, name TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY, kind TEXT NOT NULL,
            day TEXT NOT NULL, at REAL NOT NULL, state TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS event_frames(media_id TEXT NOT NULL, observation_id TEXT NOT NULL,
            kind TEXT NOT NULL, key TEXT NOT NULL, captured_at REAL NOT NULL, day TEXT NOT NULL,
            PRIMARY KEY(media_id,key));
          CREATE TABLE IF NOT EXISTS recording_transcripts(id TEXT PRIMARY KEY, source TEXT NOT NULL,
            start REAL NOT NULL, end REAL NOT NULL, text TEXT NOT NULL, media_id TEXT, meeting_id TEXT);
          CREATE INDEX IF NOT EXISTS recording_transcripts_time ON recording_transcripts(start,end);
          CREATE TABLE IF NOT EXISTS recording_meetings(id TEXT PRIMARY KEY, start REAL NOT NULL,
            end REAL, marked_at REAL);
          CREATE INDEX IF NOT EXISTS event_frames_key ON event_frames(key,captured_at);
          CREATE INDEX IF NOT EXISTS event_frames_day ON event_frames(day);
        """)
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(media)')}
        if 'storage_class' not in columns:
            # Existing originals keep their former expiry while their analyzed
            # portion is reclaimed. Only new spool writes use the new quota.
            self.db.execute("ALTER TABLE media ADD COLUMN storage_class TEXT NOT NULL DEFAULT 'legacy'")
            self.db.execute("UPDATE media SET analyzed=0 WHERE analyzed=-1 AND path IS NOT NULL")
        if 'size_bytes' not in columns:
            self.db.execute('ALTER TABLE media ADD COLUMN size_bytes INTEGER NOT NULL DEFAULT 0')
        if 'complete' not in columns:
            self.db.execute('ALTER TABLE media ADD COLUMN complete INTEGER NOT NULL DEFAULT 1')
        if 'samples' not in columns:self.db.execute('ALTER TABLE media ADD COLUMN samples TEXT')
        for name, kind in (("meeting_id", "TEXT"), ("retained_until", "REAL")):
            if name not in columns:self.db.execute(f'ALTER TABLE media ADD COLUMN {name} {kind}')
        for row in self.db.execute('SELECT id,COALESCE(path,preserved) AS path FROM media WHERE (path IS NOT NULL OR preserved IS NOT NULL) AND size_bytes=0').fetchall():
            try: size = Path(row['path']).stat().st_size
            except FileNotFoundError: size = 0
            self.db.execute('UPDATE media SET size_bytes=? WHERE id=?', (size,row['id']))
        self.db.execute('CREATE INDEX IF NOT EXISTS media_storage ON media(storage_class,analyzed) WHERE path IS NOT NULL')
        self.db.execute('CREATE INDEX IF NOT EXISTS observation_media_source ON observation_media(media_id)')
        self.db.commit()
        self.cleanup_error = None
        self.event_frame_error = None
        self._reconcile_event_frames()
        self.spool_blocked = False
        self.last_orphan_scan = None
        tracked = {Path(row['path']).name for row in self.db.execute('SELECT path FROM media WHERE path IS NOT NULL')}
        self.orphan_bytes = sum(path.stat().st_size for path in (self.root/'raw').iterdir()
            if path.name not in tracked and path.is_file())

    def execute(self, sql, params=()):
        with self.lock:
            try:
                cursor = self.db.execute(sql, params)
                rows = [dict(row) for row in cursor.fetchall()] if cursor.description else []
                self.db.commit()
                return rows
            except sqlite3.Error:
                self.db.rollback()
                raise

    def get(self, key, default=None):
        rows = self.execute("SELECT value FROM settings WHERE key=?", (key,))
        return json.loads(rows[0]["value"]) if rows else default

    def set(self, key, value):
        self.execute("INSERT OR REPLACE INTO settings VALUES (?,?)", (key, json.dumps(value)))

    def media(self, source, start, end, data: bytes, suffix, *, temporary=False, complete=True, samples=None):
        if source not in {"camera", "microphone", "screen", "system-audio"} or end < start:
            raise ValueError("invalid media interval/source")
        if not complete and (source!='camera' or not temporary):raise ValueError('only temporary camera clips may remain open')
        mid = uuid.uuid4().hex
        path = self.root / "raw" / f"{mid}.{suffix}"
        with self.lock:
            if temporary:self.admit_spool(len(data))
            created = False
            try:
                with path.open("xb") as stream:
                    created = True
                    os.chmod(path, 0o600)
                    stream.write(data)
                self.execute("INSERT INTO media(id,source,start,end,path,storage_class,size_bytes,complete,samples) VALUES (?,?,?,?,?,?,?,?,?)",
                             (mid, source, start, end, str(path), 'spool' if temporary else 'requested', len(data),int(complete),json.dumps(samples) if samples else None))
            except (OSError,sqlite3.Error):
                if created:path.unlink(missing_ok=True)
                raise
        return mid

    def append_camera(self,mid,data,end,samples):
        with self.lock:
            rows=self.execute("SELECT * FROM media WHERE id=? AND source='camera' AND storage_class='spool' AND complete=0 AND path IS NOT NULL",(mid,))
            if not rows:raise ValueError('camera clip is unavailable or already complete')
            row=rows[0]
            if end<row['end']:raise ValueError('camera append moved backwards')
            self.admit_spool(len(data))
            path=Path(row['path'])
            if path.parent.resolve()!=(self.root/'raw').resolve() or path.is_symlink():raise ValueError('unexpected-original-path')
            with path.open('r+b') as stream:
                stream.seek(0,os.SEEK_END)
                if stream.tell()!=row['size_bytes']:raise OSError('camera clip size differs from committed metadata')
                try:
                    if stream.write(data)!=len(data):raise OSError('short camera append')
                    stream.flush()
                    self.execute('UPDATE media SET end=?,size_bytes=?,samples=? WHERE id=?',
                        (end,row['size_bytes']+len(data),json.dumps(samples),mid))
                except (OSError,sqlite3.Error):
                    stream.truncate(row['size_bytes']);stream.flush()
                    raise

    def finish_camera(self,mid):
        with self.lock:
            rows=self.execute('SELECT * FROM media WHERE id=? AND complete=0 AND path IS NOT NULL',(mid,))
            if not rows:return
            row=rows[0];path=Path(row['path'])
            if path.parent.resolve()!=(self.root/'raw').resolve() or path.is_symlink():raise ValueError('unexpected-original-path')
            try:
                with path.open('r+b') as stream:
                    stream.seek(0,os.SEEK_END)
                    if stream.tell()<row['size_bytes']:raise OSError('committed camera bytes are missing')
                    stream.truncate(row['size_bytes'])
                self.execute('UPDATE media SET complete=1 WHERE id=?',(mid,))
            except FileNotFoundError:
                self.execute('UPDATE media SET path=NULL,complete=1 WHERE id=?',(mid,))
                self.merge_gap(row['source'],'capture-file-missing',row['start'],row['end'])

    def recover_camera_clips(self):
        """Call only after acquiring the service's single-instance lock.

        Opening a Store for a CLI read must never close the running service's
        clip. A damaged original is quarantined for expiry, not marked analyzed.
        Storage failure cannot prevent the microphone/control service starting.
        """
        try:rows=self.execute('SELECT * FROM media WHERE complete=0 AND path IS NOT NULL')
        except sqlite3.Error:
            self.cleanup_error='camera-recovery-unavailable'
            return
        for row in rows:
            try:self.finish_camera(row['id'])
            except (OSError,sqlite3.Error,ValueError):
                self.cleanup_error='camera-recovery-unavailable'
                try:
                    self.execute('UPDATE media SET complete=1,analyzed=-1 WHERE id=?',(row['id'],))
                    self.merge_gap('camera','capture-recovery-failed',row['start'],row['end'])
                except sqlite3.Error:pass

    def spool_bytes(self):
        return self.orphan_bytes + self.execute("SELECT COALESCE(SUM(size_bytes),0) AS bytes FROM media WHERE storage_class='spool' AND path IS NOT NULL")[0]['bytes']

    def admit_spool(self,size):
        used=self.spool_bytes()
        reserve=min(8*1024*1024,self.SPOOL_LIMIT//4)
        if size>self.SPOOL_LIMIT:raise AnalysisBufferFull(errno.ENOSPC,'analysis-buffer-full')
        if self.spool_blocked and used>self.SPOOL_LIMIT-reserve:
            raise AnalysisBufferFull(errno.ENOSPC,'analysis-buffer-full')
        self.spool_blocked=False
        if used+size>self.SPOOL_LIMIT:
            self.spool_blocked=True
            raise AnalysisBufferFull(errno.ENOSPC,'analysis-buffer-full')

    def cleanup_analyzed(self, *, mids=None, limit=100):
        """Reclaim only committed derived originals, preserving separate copies."""
        removed = 0
        with self.lock:
            query = """SELECT id,path FROM media WHERE analyzed=1 AND complete=1 AND path IS NOT NULL
                AND retained_until IS NULL AND storage_class IN ('spool','legacy') AND EXISTS (
                    SELECT 1 FROM observation_media r JOIN observations o ON o.id=r.observation_id
                    WHERE r.media_id=media.id)"""
            params = ()
            if mids is not None:
                if not mids:return 0
                query += ' AND id IN ('+','.join('?' for _ in mids)+')'
                params = tuple(mids)
            rows = self.execute(query+' ORDER BY start LIMIT ?', (*params,limit))
            self.cleanup_error = None
            deleted = []
            for row in rows:
                try:
                    path = Path(row['path'])
                    if path.parent.resolve() != (self.root/'raw').resolve():
                        raise ValueError('unexpected-original-path')
                    path.unlink(missing_ok=True)
                    deleted.append(row['id'])
                except (OSError,sqlite3.Error,ValueError):
                    self.cleanup_error = 'raw-cleanup-unavailable'
            if deleted:
                try:
                    self.execute('UPDATE media SET path=NULL WHERE id IN ('+','.join('?' for _ in deleted)+')',tuple(deleted))
                    removed = len(deleted)
                except sqlite3.Error:self.cleanup_error = 'raw-cleanup-unavailable'
        return removed

    def storage_status(self):
        with self.lock:
            rows = self.execute("SELECT storage_class,COUNT(*) AS files,COALESCE(SUM(size_bytes),0) AS bytes,MIN(start) AS oldest FROM media WHERE path IS NOT NULL GROUP BY storage_class")
            return {'mode':'analysis-first','root':str(self.root),'raw_path':str(self.root/'raw'),
                'preserved_path':str(self.root/'preserved'),'database':str(self.root/'ambient.sqlite3'),
                'spool_limit_bytes':self.SPOOL_LIMIT,'spool_bytes':self.spool_bytes(),
                'recovering_capacity':self.spool_blocked,'resume_free_bytes':min(8*1024*1024,self.SPOOL_LIMIT//4),
                'classes':rows,'cleanup_error':self.cleanup_error,
                'preserved':self.execute('SELECT COUNT(*) AS files,COALESCE(SUM(size_bytes),0) AS bytes FROM media WHERE preserved IS NOT NULL')[0],
                'pending':self.execute("SELECT COUNT(*) AS count FROM media WHERE analyzed=0 AND path IS NOT NULL")[0]['count'],
                'collecting':self.execute('SELECT COUNT(*) AS count FROM media WHERE complete=0 AND path IS NOT NULL')[0]['count'],
                'rule':'После анализа исходники удаляются; результаты и выбранные изображения сохраняются.'}

    def expire(self, now=None):
        now = time.time() if now is None else now
        count = 0
        with self.lock:
            count += self.cleanup_analyzed()
            # Start time is intentional: no byte survives 24h because a segment ended later.
            for row in self.execute("SELECT * FROM media WHERE path IS NOT NULL AND retained_until IS NULL AND start<=?", (now - 86400,)):
                Path(row["path"]).unlink(missing_ok=True)
                self.execute("UPDATE media SET path=NULL WHERE id=?", (row["id"],))
                if row['analyzed'] != 1:
                    try:self.merge_gap(row['source'],'analysis-expired',row['start'],row['end'])
                    except sqlite3.Error:self.cleanup_error='retention-gap-log-unavailable'
                count += 1
            for row in self.execute("SELECT id FROM media WHERE retained_until<=?", (now,)):
                self.delete(row["id"])
                self.execute("UPDATE media SET retained_until=NULL WHERE id=?", (row["id"],))
                count += 1
            expired = self.execute("SELECT COUNT(*) AS n FROM recording_transcripts WHERE start<=?", (now - 30*86400,))[0]["n"]
            if expired:
                self.execute("DELETE FROM recording_transcripts WHERE start<=?", (now - 30*86400,))
                # secure_delete clears freed database pages; truncate old WAL frames too.
                self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            playback = self.root / "playback"
            if playback.exists():
                for file in playback.iterdir():
                    if len(file.stem) != 32:
                        if file.stat().st_mtime <= now - 86400:file.unlink(missing_ok=True)
                        continue
                    if not self.execute("SELECT 1 FROM media WHERE id=? AND (path IS NOT NULL OR preserved IS NOT NULL)", (file.stem,)):
                        file.unlink(missing_ok=True)
            # Recover orphaned files after a process crash between file creation and INSERT.
            if self.last_orphan_scan is None or now-self.last_orphan_scan >= 60:
                tracked = {Path(r["path"]).name for r in self.execute("SELECT path FROM media WHERE path IS NOT NULL")}
                for path in (self.root / "raw").iterdir():
                    if path.name not in tracked and path.stat().st_mtime <= now - 86400:
                        path.unlink()
                self.orphan_bytes = sum(path.stat().st_size for path in (self.root/'raw').iterdir()
                    if path.name not in tracked and path.is_file())
                self.last_orphan_scan = now
        return count

    def preserve(self, mid, now=None):
        with self.lock:
            self.expire(now)
            rows = self.execute("SELECT * FROM media WHERE id=?", (mid,))
            if not rows:
                raise ValueError("original media is unavailable or expired")
            row = rows[0]
            if not row['complete']:raise ValueError('camera clip is still recording; select a completed recording')
            if row["preserved"]:
                return mid
            if not row["path"]:
                raise ValueError("original media is unavailable or expired")
            target = self.root / "preserved" / Path(row["path"]).name
            # Do not leave an untracked, partial long-term copy after ENOSPC or
            # a failed database update. Publish only a complete private copy.
            fd, temporary_name = tempfile.mkstemp(prefix=mid + "-", suffix=".tmp", dir=target.parent)
            os.close(fd)
            temporary = Path(temporary_name)
            published = False
            try:
                shutil.copyfile(row["path"], temporary)
                temporary.chmod(0o600)
                temporary.replace(target)
                published = True
                self.execute("UPDATE media SET preserved=? WHERE id=?", (str(target), mid))
            except (OSError, sqlite3.Error):
                temporary.unlink(missing_ok=True)
                if published: target.unlink(missing_ok=True)
                raise
            return mid

    def _reconcile_event_frames(self):
        """Remove event-frame files a crash left between publish and commit.

        Only files with the event-frame name prefix are considered; every
        other preserved original is untouched.
        """
        kept = {row["preserved"] for row in self.db.execute(
            "SELECT preserved FROM media WHERE storage_class='event-frame' AND preserved IS NOT NULL")}
        for path in (self.root / "preserved").glob("event-*"):
            if path.suffix == ".tmp" or str(path) not in kept:
                path.unlink(missing_ok=True)

    def keep_event_frame(self, observation_id, frame, captured_at, events, day):
        """DEC-065: durably keep one analyzed camera frame for new events.

        events are (kind, key) pairs. A key already kept within the spacing
        window is skipped; the daily limit counts frames, including frames
        the owner later deleted. The frame is a preserved camera media row,
        so the existing listing and delete controls apply. The file is synced
        and published, then its provenance committed; a failure removes it,
        and startup reconciliation removes one a crash left uncommitted.
        """
        if not frame or not events: return None
        with self.lock:
            fresh = []
            for kind, key in events:
                if (kind, key) in fresh or self.execute(
                        "SELECT 1 FROM event_frames WHERE key=? AND ABS(captured_at-?)<? LIMIT 1",
                        (key, captured_at, WINDOW_SECONDS)):
                    continue
                fresh.append((kind, key))
            if not fresh: return None
            if self.execute("SELECT COUNT(DISTINCT media_id) AS frames FROM event_frames WHERE day=?",
                            (day,))[0]["frames"] >= DAILY_LIMIT:
                return None
            mid = uuid.uuid4().hex
            target = self.root / "preserved" / f"event-{mid}.jpg"
            fd, temporary_name = tempfile.mkstemp(prefix=f"event-{mid}-", suffix=".tmp", dir=target.parent)
            temporary = Path(temporary_name)
            published = False
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(frame)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.chmod(0o600)
                temporary.replace(target)
                published = True
                directory = os.open(target.parent, os.O_RDONLY)
                try: os.fsync(directory)
                finally: os.close(directory)
                self.db.execute("BEGIN")
                self.db.execute("""INSERT INTO media(id,source,start,end,path,preserved,analyzed,storage_class,size_bytes,complete)
                    VALUES (?,'camera',?,?,NULL,?,1,'event-frame',?,1)""", (mid, captured_at, captured_at, str(target), len(frame)))
                for kind, key in fresh:
                    self.db.execute("INSERT INTO event_frames VALUES (?,?,?,?,?,?)",
                                    (mid, observation_id, kind, key, captured_at, day))
                self.db.commit()
            except (OSError, sqlite3.Error):
                try: self.db.rollback()
                except sqlite3.Error: pass
                temporary.unlink(missing_ok=True)
                if published: target.unlink(missing_ok=True)
                raise
            return mid

    def event_frame_status(self, day):
        with self.lock:
            kept = self.execute("""SELECT COUNT(*) AS files,COALESCE(SUM(size_bytes),0) AS bytes FROM media
                WHERE storage_class='event-frame' AND preserved IS NOT NULL""")[0]
            today = self.execute("SELECT COUNT(DISTINCT media_id) AS frames FROM event_frames WHERE day=?", (day,))[0]["frames"]
            return {**kept, "today": today, "daily_limit": DAILY_LIMIT, "spacing_seconds": WINDOW_SECONDS,
                    "error": self.event_frame_error,
                    "rule": "Ключевые кадры: человек, собака, книга или смена занятия; хранятся до удаления владельцем."}

    def delete(self, mid, *, results=False):
        with self.lock:
            for row in self.execute("SELECT * FROM media WHERE id=?", (mid,)):
                for key in ("path", "preserved"):
                    if row[key]:
                        Path(row[key]).unlink(missing_ok=True)
            self.execute("UPDATE media SET path=NULL,preserved=NULL WHERE id=?", (mid,))
            (self.root / "playback" / (mid + ".mp4")).unlink(missing_ok=True)
            if results:
                self.execute("DELETE FROM observations WHERE media_id=? OR id IN (SELECT observation_id FROM observation_media WHERE media_id=?)", (mid, mid))
                self.execute("DELETE FROM observation_media WHERE observation_id NOT IN (SELECT id FROM observations)")

    def retain_transcript(self, key, source, start, end, text, *, media_id=None, meeting_id=None, now=None):
        """Separate local speech record; neither facts export nor MCP reads this table."""
        now = time.time() if now is None else now
        if source not in {"microphone", "system-audio"} or end < start:
            raise ValueError("invalid transcript interval/source")
        if not isinstance(text, str) or not text.strip() or start <= now - 30*86400:
            return
        self.execute("INSERT OR REPLACE INTO recording_transcripts VALUES (?,?,?,?,?,?,?)",
                     (key, source, start, end, text.strip(), media_id, meeting_id))

    def tag_meeting_media(self, mid, meeting_id, *, retain=False):
        with self.lock:
            meeting = self.execute("SELECT * FROM recording_meetings WHERE id=?", (meeting_id,))
            if not meeting:return
            row = self.execute("SELECT start FROM media WHERE id=?", (mid,))[0]
            marked = meeting[0]["marked_at"]
            expiry = row["start"] + 30*86400 if retain and marked is not None and row["start"] >= marked else None
            # Move retained originals outside the bounded analysis spool. Keep
            # the original bytes/path; expiry never depends on analysis success.
            self.execute("UPDATE media SET meeting_id=?,retained_until=?,storage_class=CASE WHEN ? IS NOT NULL THEN 'meeting' ELSE storage_class END WHERE id=?",
                         (meeting_id, expiry, expiry, mid))

    def save_observation(self, mid, facts, covered_ids=None):
        with self.lock:
            ids = covered_ids or [mid]
            media = [self.execute("SELECT * FROM media WHERE id=?", (item,))[0] for item in ids]
            if any(not row['complete'] for row in media):raise ValueError('an incomplete camera clip cannot be marked analyzed')
            row = media[0]
            oid = uuid.uuid4().hex
            # No original may disappear after a partial facts/provenance write.
            try:
                self.db.execute('BEGIN')
                self.db.execute("INSERT INTO observations VALUES (?,?,?,?,?,?)",
                    (oid, mid, row["source"], min(m["start"] for m in media), max(m["end"] for m in media), json.dumps(facts)))
                for item in ids:
                    self.db.execute("INSERT INTO observation_media VALUES (?,?)", (oid,item))
                    self.db.execute("UPDATE media SET analyzed=1 WHERE id=?", (item,))
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise
            self.cleanup_analyzed(mids=ids,limit=len(ids))
            return oid

    def history(self, start, end):
        self.expire()
        corrections = {r["subject"]: r["name"] for r in self.execute("SELECT * FROM corrections")}
        rows = self.execute("""SELECT o.*, (m.path IS NOT NULL OR m.preserved IS NOT NULL) AS original_available
            FROM observations o LEFT JOIN media m ON m.id=o.media_id
            WHERE o.end>=? AND o.start<? ORDER BY o.start,o.id""", (start, end))
        for row in rows:
            refs = self.execute("SELECT m.id, (m.path IS NOT NULL OR m.preserved IS NOT NULL) AS available FROM media m JOIN observation_media r ON r.media_id=m.id WHERE r.observation_id=?", (row["id"],))
            row["source_ids"] = [r["id"] for r in refs] or [row["media_id"]]
            if refs:
                row["original_available"] = any(r["available"] for r in refs)
            row["facts"] = json.loads(row["facts"])
            for fact in row["facts"]:
                if fact.get("subject") in corrections:
                    fact["identity"] = corrections[fact["subject"]]
                    fact["identity_basis"] = "owner-confirmed"
        return rows

    def event(self, source, reason, start, end=None):
        # Reasons are controlled codes only. Never log detector output, identities or media.
        self.execute("INSERT INTO events(start,end,source,reason) VALUES (?,?,?,?)",
                     (start, end, source, reason))

    def merge_gap(self, source, reason, start, end):
        """Coalesce only adjoining, forward source gaps, never bridge a pause."""
        with self.lock:
            rows=self.execute('SELECT id,start,end FROM events WHERE source=? AND reason=? ORDER BY id DESC LIMIT 1',(source,reason))
            previous=rows[0] if rows else None
            if (previous and previous['end'] is not None and start>=previous['start']
                    and end>=previous['end'] and start<=previous['end']+.25):
                self.execute('UPDATE events SET end=? WHERE id=?',(end,previous['id']))
            else:self.event(source,reason,start,end)

    def close(self):
        with self.lock:
            self.db.close()
