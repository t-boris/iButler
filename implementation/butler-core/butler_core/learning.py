"""Traceable dialogue evidence. Metadata and expiring private text never share a DB.

The metadata DB may be backed up; ``private_root`` must be excluded. Audio is
never accepted. All expiry is measured from the original episode timestamp.
"""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
import time
import uuid
from pathlib import Path

ERRORS = {"capture", "recognition", "identity", "addressee", "turn_timing", "interruption", "context",
          "retrieval", "stale_memory", "reasoning", "execution", "style", "timing", "privacy", "unknown"}
CORRECTIONS = ERRORS | {"not_addressed", "missed_address", "unfinished", "retract", "label_addressed", "label_other"}
OUTCOMES = {"complied", "violated", "unknown", "not_applicable", "not_exercised"}
DECISION_KEYS = {"outcome", "addressed", "accepted", "confidence", "judge", "attention", "mode", "source",
                 "wake_name", "direct", "direct_name", "phase", "reason", "rule", "profile", "first_word_ms",
                 "model", "end_of_turn", "named", "scene", "operation", "completed_operation"}


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def metadata(value):
    clean = {}
    for key, item in value.items():
        if key not in DECISION_KEYS: continue
        if key == "scene" and isinstance(item, dict):
            clean[key] = "company" if item.get("other_people_present") else "alone-facing" if item.get("owner_facing_mac") else "camera-uncertain"
        elif item is None or isinstance(item, bool): clean[key] = item
        elif type(item) in (int, float) and math.isfinite(item): clean[key] = item
        elif isinstance(item, str): clean[key] = item[:120]
    return clean


class LearningStore:
    def __init__(self, metadata_path, private_root, *, clock=time.time):
        self.clock, self.lock = clock, threading.RLock()
        self.path = Path(metadata_path)
        self.private_root = Path(private_root)
        for folder in (self.path.parent, self.private_root): folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.private_root.chmod(0o700)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.private = sqlite3.connect(self.private_root / "episodes.sqlite3", check_same_thread=False)
        self.private.row_factory = sqlite3.Row
        for connection in (self.db, self.private):
            connection.execute("PRAGMA secure_delete=ON")
            connection.execute("PRAGMA busy_timeout=100")
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS episodes(id TEXT PRIMARY KEY, turn_id TEXT UNIQUE, session_id TEXT,
          born REAL, source TEXT, scope TEXT, labelled INTEGER DEFAULT 0, deleted INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS decisions(episode TEXT, revision INTEGER, data TEXT, at REAL,
          PRIMARY KEY(episode,revision));
        CREATE TABLE IF NOT EXISTS annotations(event TEXT PRIMARY KEY, id TEXT UNIQUE, episode TEXT,
          revision INTEGER, kind TEXT, cause TEXT, confidence REAL, binding TEXT, state TEXT, at REAL);
        CREATE TABLE IF NOT EXISTS correction_applications(event TEXT PRIMARY KEY, request_hash TEXT, repaired INTEGER, at REAL);
        CREATE TABLE IF NOT EXISTS receipts(id TEXT PRIMARY KEY, episode TEXT, revision INTEGER,
          profile TEXT, data TEXT, at REAL);
        CREATE TABLE IF NOT EXISTS assessments(id TEXT PRIMARY KEY, receipt TEXT, rule TEXT,
          revision INTEGER, outcome TEXT, evidence TEXT, at REAL);
        CREATE TABLE IF NOT EXISTS evaluations(version TEXT PRIMARY KEY, baseline TEXT, data TEXT,
          state TEXT, reason TEXT, at REAL);
        CREATE TABLE IF NOT EXISTS baselines(version TEXT, data_version TEXT, metrics TEXT, at REAL,
          PRIMARY KEY(version,data_version));
        CREATE TABLE IF NOT EXISTS deletions(episode TEXT PRIMARY KEY, at REAL);
        CREATE TABLE IF NOT EXISTS attention(at REAL,state TEXT,cause TEXT,UNIQUE(at,state,cause));
        """)
        self.private.execute("CREATE TABLE IF NOT EXISTS texts(id TEXT PRIMARY KEY, original TEXT, corrected TEXT, born REAL, expires REAL)")
        self.private.execute("CREATE TABLE IF NOT EXISTS fragments(episode TEXT,id TEXT,data TEXT,expires REAL,PRIMARY KEY(episode,id))")
        self.private.execute("CREATE TABLE IF NOT EXISTS correction_texts(event TEXT PRIMARY KEY,episode TEXT,text TEXT,expires REAL)")
        self.private.execute("CREATE TABLE IF NOT EXISTS transcripts(episode TEXT,revision INTEGER,text TEXT,state TEXT,born REAL,expires REAL,PRIMARY KEY(episode,revision))")
        self.db.commit(); self.private.commit()
        self.path.chmod(0o600); (self.private_root / "episodes.sqlite3").chmod(0o600)
        self.expire()

    def close(self):
        self.db.close(); self.private.close()

    def episode(self, identifier, turn, session, born, text, *, source="microphone", scope="general"):
        if source not in {"microphone", "owner-panel"}: raise ValueError("ineligible feedback source")
        with self.lock:
            if self.db.execute("SELECT 1 FROM deletions WHERE episode=?", (identifier,)).fetchone(): return
            self.db.execute("INSERT OR IGNORE INTO episodes(id,turn_id,session_id,born,source,scope) VALUES(?,?,?,?,?,?)",
                            (identifier, turn, session, born, source, scope))
            self.db.commit()
            original = self.db.execute("SELECT born,labelled FROM episodes WHERE id=?", (identifier,)).fetchone()
            expires = original["born"] + (30 if original["labelled"] else 1) * 86400
            if self.clock() < expires:
                self.private.execute("INSERT OR IGNORE INTO texts VALUES(?,?,NULL,?,?)", (identifier, text[:12000], original["born"], expires))
                self.private.commit()

    def decide(self, episode, fields, revision=None):
        with self.lock:
            if fields.get("mode") in {"private", "general"}:
                self.db.execute("UPDATE episodes SET scope=? WHERE id=?", (fields["mode"], episode))
            if revision is not None:
                data = encode(metadata(fields))
                old = self.db.execute("SELECT data FROM decisions WHERE episode=? AND revision=?", (episode, revision)).fetchone()
                if old and old[0] != data: raise ValueError("decision revision is immutable")
                self.db.execute("INSERT OR IGNORE INTO decisions VALUES(?,?,?,?)", (episode, revision, data, self.clock()))
                self.db.commit()
                return revision
            old = self.db.execute("SELECT revision,data FROM decisions WHERE episode=? ORDER BY revision DESC LIMIT 1", (episode,)).fetchone()
            combined = {**(json.loads(old["data"]) if old else {}), **metadata(fields)}
            revision = old["revision"] + 1 if old else 1
            if old and old["data"] == encode(combined): return old["revision"]
            self.db.execute("INSERT INTO decisions VALUES(?,?,?,?)", (episode, revision, encode(combined), self.clock()))
            self.db.commit()
            return revision

    def transcript(self, episode, revision, text, *, born, state):
        """Draft/final revisions stay entirely outside the metadata backup.

        Their deadline is the original capture's, including when finalization
        or a delayed persistence retry happens much later.
        """
        with self.lock:
            if self.db.execute('SELECT 1 FROM deletions WHERE episode=?', (episode,)).fetchone(): return
            original = self.db.execute('SELECT born,labelled FROM episodes WHERE id=?', (episode,)).fetchone()
            first = self.private.execute('SELECT MIN(born) FROM transcripts WHERE episode=?', (episode,)).fetchone()[0]
            born = min(born, first) if first is not None else born
            if original is not None: born = min(born, original['born'])
            expires = born + (30 if original is not None and original['labelled'] else 1)*86400
            if self.clock() >= expires: return
            self.private.execute('INSERT OR IGNORE INTO transcripts VALUES(?,?,?,?,?,?)',
                                 (episode, revision, text[:12000], state, born, expires))
            self.private.commit()

    def annotate(self, event, episode, revision, kind, *, cause="unknown", confidence=None,
                 binding="selected-panel-object", corrected=None, authorized=False, retract=None, identifier=None,
                 repaired=False, client_hash=None):
        if not authorized: raise PermissionError("owner authorization required")
        if kind not in CORRECTIONS or cause not in ERRORS: raise ValueError("invalid correction")
        if confidence is not None and (type(confidence) not in (int, float) or not 0 <= confidence <= 1):
            raise ValueError("invalid cause confidence")
        with self.lock:
            request_hash = client_hash or hashlib.sha256(encode([episode, revision, kind, cause, confidence, binding, corrected, retract]).encode()).hexdigest()
            old = self.db.execute("SELECT * FROM annotations WHERE event=?", (event,)).fetchone()
            if old:
                if (old["episode"], old["revision"], old["kind"]) != (episode, revision, kind):
                    raise ValueError("correction event reused")
                application = self.db.execute("SELECT request_hash FROM correction_applications WHERE event=?", (event,)).fetchone()
                if application and application[0] != request_hash: raise ValueError("correction content changed on retry")
                return dict(old)
            supported = self.db.execute("SELECT 1 FROM decisions WHERE episode=? AND revision=?", (episode, revision)).fetchone()
            state = "captured" if supported and binding != "ambiguous" else "unresolved"
            identifier = identifier or "correction-" + uuid.uuid4().hex
            self.db.execute("INSERT INTO annotations VALUES(?,?,?,?,?,?,?,?,?,?)", (event, identifier, episode, revision,
                            kind, cause, confidence, binding, state, self.clock()))
            self.db.execute("INSERT INTO correction_applications VALUES(?,?,?,?)", (event, request_hash, bool(repaired), self.clock()))
            if kind == "retract":
                self.db.execute("UPDATE annotations SET state='retracted' WHERE id=? AND episode=?", (retract, episode))
            elif state == "captured":
                self.db.execute("UPDATE episodes SET labelled=1 WHERE id=?", (episode,))
                born = self.db.execute("SELECT born FROM episodes WHERE id=?", (episode,)).fetchone()[0]
                # UPDATE cannot recreate text which already expired.
                self.private.execute("UPDATE texts SET expires=?,corrected=COALESCE(?,corrected) WHERE id=? AND expires>?",
                                     (born + 30 * 86400, corrected if kind == "recognition" else None, episode, self.clock()))
                self.private.execute("UPDATE fragments SET expires=? WHERE episode=? AND expires>?", (born + 30 * 86400, episode, self.clock()))
                self.private.execute("UPDATE transcripts SET expires=? WHERE episode=? AND expires>?", (born + 30 * 86400, episode, self.clock()))
                if corrected is not None and self.private.execute("SELECT 1 FROM texts WHERE id=? AND expires>?", (episode, self.clock())).fetchone():
                    self.private.execute("INSERT OR IGNORE INTO correction_texts VALUES(?,?,?,?)", (event, episode, corrected, born + 30 * 86400))
                self.private.commit()
            self.db.commit()
            return dict(self.db.execute("SELECT * FROM annotations WHERE event=?", (event,)).fetchone())

    def text(self, identifier):
        self.expire()
        with self.lock:
            row = self.private.execute("SELECT * FROM texts WHERE id=?", (identifier,)).fetchone()
            return dict(row) if row else None

    def expire(self):
        with self.lock:
            self.private.execute("DELETE FROM texts WHERE expires<=?", (self.clock(),))
            for row in self.db.execute("SELECT episode FROM deletions"):
                self.private.execute("DELETE FROM texts WHERE id=?", (row[0],))
                self.private.execute("DELETE FROM transcripts WHERE episode=?", (row[0],))
            self.private.execute("DELETE FROM fragments WHERE expires<=? OR episode NOT IN (SELECT id FROM texts)", (self.clock(),))
            self.private.execute("DELETE FROM correction_texts WHERE expires<=? OR episode NOT IN (SELECT id FROM texts)", (self.clock(),))
            self.private.execute("DELETE FROM transcripts WHERE expires<=?", (self.clock(),))
            self.private.commit()

    def save_fragments(self, episode, fragments):
        with self.lock:
            current = self.private.execute("SELECT expires FROM texts WHERE id=? AND expires>?", (episode, self.clock())).fetchone()
            if current is None: return
            for fragment in fragments:
                self.private.execute("INSERT OR REPLACE INTO fragments VALUES(?,?,?,?)", (episode, fragment["id"], encode(fragment), current[0]))
            self.private.commit()

    def fragments(self, episode):
        self.expire()
        with self.lock:
            return [json.loads(row[0]) for row in self.private.execute("SELECT data FROM fragments WHERE episode=?", (episode,))]

    def delete(self, identifier):
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO deletions VALUES(?,?)", (identifier, self.clock()))
            receipt_ids = [r[0] for r in self.db.execute("SELECT id FROM receipts WHERE episode=?", (identifier,))]
            for receipt in receipt_ids: self.db.execute("DELETE FROM assessments WHERE receipt=?", (receipt,))
            for table in ("annotations", "receipts", "decisions"):
                if table == "annotations":
                    self.db.execute("DELETE FROM correction_applications WHERE event IN (SELECT event FROM annotations WHERE episode=?)", (identifier,))
                self.db.execute(f"DELETE FROM {table} WHERE episode=?", (identifier,))
            self.db.execute("DELETE FROM episodes WHERE id=?", (identifier,))
            self.db.commit(); self.expire()
            return {"deleted": True, "removed": ["private-text", "annotations", "decisions", "receipts", "assessments"]}

    def receipt(self, episode, revision, profile, *, rules=(), memories=(), excluded=(), export="local"):
        if export not in {"local", "owner-addressed", "anonymized-feedback"}: raise ValueError("invalid export mode")
        refs = lambda rows: [{k: row[k] for k in ("id", "revision", "hash", "reason") if k in row} for row in rows]
        record = {"rules": refs(rules), "memories": refs(memories), "excluded": refs(excluded), "export": export}
        identifier = hashlib.sha256(encode([episode, revision, profile, record]).encode()).hexdigest()
        with self.lock:
            self.db.execute("INSERT OR IGNORE INTO receipts VALUES(?,?,?,?,?,?)", (identifier, episode, revision, profile, encode(record), self.clock()))
            self.db.commit()
        return identifier

    def assess(self, receipt, rule, revision, outcome, *, evidence=None, applicable=None, exercised=True):
        if outcome not in OUTCOMES: raise ValueError("invalid compliance outcome")
        if applicable is False: outcome = "not_applicable"
        elif not exercised: outcome = "not_exercised"
        elif applicable is not True or not evidence: outcome = "unknown"
        with self.lock:
            row = self.db.execute("SELECT data FROM receipts WHERE id=?", (receipt,)).fetchone()
            if row is None: raise ValueError("application receipt missing")
            if not any(r.get("id") == rule and r.get("revision") == revision for r in json.loads(row[0])["rules"]):
                raise ValueError("rule revision was not applied")
            identifier = uuid.uuid4().hex
            # Store an evidence reference/hash, never feedback prose.
            reference = hashlib.sha256(str(evidence).encode()).hexdigest() if evidence else None
            self.db.execute("INSERT INTO assessments VALUES(?,?,?,?,?,?,?)", (identifier, receipt, rule, revision, outcome, reference, self.clock()))
            self.db.commit()
        return outcome

    def rule_uses(self, identifier, limit=5):
        """Metadata examples; receipt alone never establishes compliance."""
        out = []
        with self.lock:
            for row in self.db.execute("SELECT * FROM receipts ORDER BY at DESC LIMIT 1000"):
                data = json.loads(row["data"])
                for rule in data["rules"]:
                    if rule.get("id") != identifier: continue
                    assessment = self.db.execute("SELECT outcome FROM assessments WHERE receipt=? AND rule=? AND revision=? ORDER BY at DESC LIMIT 1",
                        (row["id"], identifier, rule["revision"])).fetchone()
                    out.append({"at": row["at"], "revision": rule["revision"], "outcome": assessment[0] if assessment else "unknown"})
                    if len(out) >= limit: return out
        return out

    def dataset(self, since=0):
        with self.lock:
            rows = self.db.execute("SELECT e.*,d.revision,d.data FROM episodes e JOIN decisions d ON d.episode=e.id "
                                   "WHERE e.born>=? AND (d.revision=(SELECT MAX(revision) FROM decisions WHERE episode=e.id) "
                                   "OR EXISTS(SELECT 1 FROM annotations a WHERE a.episode=e.id AND a.revision=d.revision AND a.state='captured' "
                                   "AND a.kind IN ('label_addressed','label_other','not_addressed','missed_address')))", (since,)).fetchall()
            out = []
            for row in rows:
                label = self.db.execute("SELECT kind FROM annotations WHERE episode=? AND revision=? AND state='captured' "
                                        "AND kind IN ('label_addressed','label_other','not_addressed','missed_address') ORDER BY at DESC,rowid DESC LIMIT 1",
                                        (row["id"], row["revision"])).fetchone()
                verdict = json.loads(row["data"])
                expected = None if label is None else label[0] in {"label_addressed", "missed_address"}
                split = "control" if int(hashlib.sha256(row["session_id"].encode()).hexdigest()[:8], 16) % 5 == 0 else "training"
                out.append({"episode": row["id"], "revision": row["revision"], "session": row["session_id"], "split": split,
                            "label": expected, "accepted": verdict.get("accepted"), "named": bool(verdict.get("wake_name")),
                            "scene": verdict.get("scene") or "unknown", "first_word_ms": verdict.get("first_word_ms"),
                            "interruption": verdict.get("outcome") == "noise-interruption"})
            return out

    @staticmethod
    def metrics(rows):
        def rate(pool, error):
            known = [r for r in pool if type(r.get("accepted")) is bool]
            count = sum(error(r) for r in known)
            return {"errors": count, "denominator": len(known), "rate": count / len(known) if known else None}
        labelled = [r for r in rows if r["label"] is not None]
        addressed = [r for r in labelled if r["label"]]
        other = [r for r in labelled if not r["label"]]
        latencies = sorted(r["first_word_ms"] for r in rows if type(r.get("first_word_ms")) in (int, float))
        import statistics
        scenes = {}
        for row in rows: scenes[row["scene"]] = scenes.get(row["scene"], 0) + 1
        return {"sample_size": len(rows), "sample_unit": "decision revision", "episodes": len({r.get("episode") for r in rows}),
                "labelled": len(labelled), "unknown_outcomes": len(rows) - len(labelled),
                "unknown_share": (len(rows) - len(labelled)) / len(rows) if rows else None, "scenes": scenes,
                "false_interventions": rate(other, lambda r: r["accepted"]),
                "missed_addresses": rate(addressed, lambda r: not r["accepted"]),
                "named_misses": rate([r for r in addressed if r["named"]], lambda r: not r["accepted"]),
                "unnamed_misses": rate([r for r in addressed if not r["named"]], lambda r: not r["accepted"]),
                "noise_or_other_voice_interruptions": sum(r.get("interruption", False) for r in rows),
                "median_first_word_ms": statistics.median(latencies) if latencies else None,
                "acoustic_verification": "unavailable: no retained feedback audio"}

    def report(self):
        return self.metrics(self.dataset(self.clock() - 7 * 86400))

    def anonymized_summary(self):
        report = self.report()
        # Construct from fixed aggregate keys. No free strings, IDs, timestamps,
        # transcripts, corrected words, call conclusions or names can enter.
        return {"sample_size": report["sample_size"], "labelled": report["labelled"],
                "unknown_outcomes": report["unknown_outcomes"],
                "false_interventions": report["false_interventions"], "missed_addresses": report["missed_addresses"]}

    def record_baseline(self, version, metrics, *, data_version):
        self.validate_metrics(metrics)
        with self.lock:
            old = self.db.execute("SELECT metrics FROM baselines WHERE version=? AND data_version=?", (version, data_version)).fetchone()
            if old and old[0] != encode(metrics): raise ValueError("baseline is immutable")
            self.db.execute("INSERT OR IGNORE INTO baselines VALUES(?,?,?,?)", (version, data_version, encode(metrics), self.clock()))
            self.db.commit()

    @staticmethod
    def validate_metrics(metrics):
        for key in ("false_interventions", "missed_addresses"):
            value = metrics[key]
            if type(value.get("denominator")) is not int or value["denominator"] < 0: raise ValueError("invalid denominator")
            rate = value.get("rate")
            if rate is not None and (type(rate) not in (float, int) or not math.isfinite(rate) or not 0 <= rate <= 1):
                raise ValueError("invalid error rate")
            if value["denominator"] and rate is None: raise ValueError("missing error rate")
        latency = metrics.get("median_first_word_ms")
        if latency is not None and (type(latency) not in (int, float) or not math.isfinite(latency) or latency < 0):
            raise ValueError("invalid latency")

    def evaluate(self, version, baseline, before, after, *, data_version, training_sessions, control_sessions):
        self.validate_metrics(before); self.validate_metrics(after)
        if set(training_sessions) & set(control_sessions): raise ValueError("session leakage")
        recorded = self.db.execute("SELECT metrics FROM baselines WHERE version=? AND data_version=?", (baseline, data_version)).fetchone()
        if recorded is None or recorded[0] != encode(before): raise ValueError("recorded baseline for this data version required")
        names = ("false_interventions", "missed_addresses")
        comparable = all(before[k].get("denominator", 0) >= 2 and after[k].get("denominator", 0) == before[k]["denominator"] for k in names)
        latency_known = before.get("median_first_word_ms") is not None and after.get("median_first_word_ms") is not None
        worse = comparable and any(after[k]["rate"] > before[k]["rate"] for k in names)
        worse = worse or comparable and (after["false_interventions"]["rate"] > .02 or after["missed_addresses"]["rate"] > .05)
        worse = worse or latency_known and after["median_first_word_ms"] > before["median_first_word_ms"]
        state = "rejected" if worse else "evaluated" if comparable and latency_known else "insufficient-evidence"
        data = {"before": before, "after": after, "data_version": data_version,
                "training_sessions": training_sessions, "control_sessions": control_sessions}
        with self.lock:
            self.db.execute("INSERT OR REPLACE INTO evaluations VALUES(?,?,?,?,?,?)", (version, baseline, encode(data), state, None, self.clock()))
            self.db.commit()
        return state

    def activate(self, version):
        with self.lock:
            row = self.db.execute("SELECT * FROM evaluations WHERE version=?", (version,)).fetchone()
            if row is None or row["state"] != "evaluated": raise ValueError("baseline evaluation required")
            self.db.execute("UPDATE evaluations SET state='active' WHERE version=?", (version,)); self.db.commit()
        return version

    def rollback(self, version, reason):
        if not reason: raise ValueError("rollback reason required")
        with self.lock:
            row = self.db.execute("SELECT baseline FROM evaluations WHERE version=? AND state='active'", (version,)).fetchone()
            if row is None: raise ValueError("version not active")
            self.db.execute("UPDATE evaluations SET state='rolled-back',reason=? WHERE version=?", (reason[:120], version))
            self.db.execute("UPDATE evaluations SET state='active' WHERE version=?", (row[0],)); self.db.commit()
            return row[0]
