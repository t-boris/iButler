"""Owner corrections: immediate repair first, bounded asynchronous persistence second.

The worker owns SQLite and vault I/O; the audio and conversation threads only
update bounded memory and enqueue immutable snapshots. No feedback audio is kept.
"""
from __future__ import annotations

import hashlib
import json
import queue
import re
import threading
import time
import uuid
from collections import deque
from pathlib import Path

KINDS = {"not_addressed", "missed_address", "recognition", "unfinished", "reasoning", "stale_memory", "execution",
         "capture", "identity", "addressee", "turn_timing", "interruption", "context", "retrieval", "style", "timing",
         "privacy", "unknown", "retract", "label_addressed", "label_other"}
VOICE_CUES = ((r"я\s+не\s+тебе\s+(?:говорил|говорила)", "not_addressed"),
              (r"это\s+было\s+тебе", "missed_address"),
              (r"я\s+ещ[её]\s+не\s+закончил[а]?", "unfinished"),
              (r"я\s+сказал[а]?\s+другое", "recognition"),
              (r"(?:использована\s+неверная|неверная)\s+память", "stale_memory"))


class DialogueFeedback:
    def __init__(self, service):
        self.service = service
        self.lock = threading.RLock()
        self.rows = deque(maxlen=80)
        self.events = {}
        self.jobs = queue.PriorityQueue(maxsize=256)
        self.serial = 0
        self.session = uuid.uuid4().hex
        self.last_at = float("-inf")
        self.by_token = {}
        self.failed = 0
        self.error = None
        self.storage = None
        self.rule_store = None
        self.thread = None
        self.stopped = threading.Event()
        self.epoch = 0
        self.receipts = deque(maxlen=80)
        self.report = {"sample_size": 0, "labelled": 0, "unknown_outcomes": 0}
        self.profile = {"version": "unavailable", "selected": [], "excluded": []}
        self.last_profile_refresh = 0
        self.contexts = {}
        self.retry_jobs = deque(maxlen=256)
        self.retry_at = 0
        self.forced_tokens = set()
        self.export_cancel = threading.Event()
        self.next_forced = set()
        self.exclusion_origins = {}
        self.agent_requests = {}

    def ensure(self, token, text, mode, *, source="microphone"):
        if source not in {"microphone", "owner-panel"}: return
        with self.lock:
            if token in self.by_token: return
        identifier = self.capture(text, at=self.service.clock(), mode=mode, token=token)
        self.decision(identifier, {"mode": mode, "source": source, "outcome": "recognized"})

    def forced(self, token):
        with self.lock: return token in self.forced_tokens

    def start(self):
        if self.thread is None:
            self.thread = threading.Thread(target=self._loop, name="butler-feedback", daemon=True)
            self.thread.start()

    def _queue(self, command, payload, priority=10):
        with self.lock:
            self.serial += 1
            try: self.jobs.put_nowait((priority, self.serial, self.epoch, command, payload))
            except queue.Full:
                self.failed += 1; self.error = "feedback-backlog-full"
                if priority == 0:
                    if len(self.retry_jobs) >= 256: self.retry_jobs.pop()
                    self.retry_jobs.appendleft((priority, self.serial, self.epoch, command, payload))

    def transcript(self, revision):
        """Private, expiring text only; a draft creates no decision/action."""
        self._queue('transcript', {k: revision[k] for k in ('id', 'born', 'revision', 'text', 'state')})
        self.start()

    def capture(self, text, *, at, mode="general", token=None, identifier=None, born=None):
        with self.lock:
            if identifier and any(r['id'] == identifier for r in self.rows): return identifier
            if at - self.last_at > 300: self.session = uuid.uuid4().hex
            self.last_at = at
            identifier = identifier or "utterance-" + uuid.uuid4().hex
            row = {"id": identifier, "turn": "turn-" + uuid.uuid4().hex, "session": self.session,
                   "at": at if born is None else born, "mode": mode, "text": text[:12000], "original": text[:12000], "revision": 0,
                   "decision": {}, "revisions": [], "annotations": [], "memories": [], "saved": False}
            self.rows.append(row)
            retained = {r["id"] for r in self.rows}
            self.by_token = {t: value for t, value in self.by_token.items() if value in retained}
            self.forced_tokens.intersection_update(self.by_token)
            self.contexts = {key: value for key, value in self.contexts.items() if key in retained}
            if token is not None: self.by_token[token] = identifier
            self._queue("episode", dict(row))
            self.start()
            return identifier

    def attach(self, token, identifier):
        with self.lock:
            self.by_token[token] = identifier
            if identifier in self.next_forced:
                self.next_forced.discard(identifier); self.forced_tokens.add(token)

    def decision(self, identifier, fields):
        from butler_core.learning import metadata
        with self.lock:
            row = next((r for r in self.rows if r["id"] == identifier), None)
            if row is None: return
            clean = {**row["decision"], **metadata(fields)}
            if clean == row["decision"]: return
            row["revision"] += 1
            row["decision"] = clean
            if clean.get("mode") in {"private", "general"}: row["mode"] = clean["mode"]
            row["revisions"].append(dict(clean))
            self._queue("decision", {"id": identifier, "revision": row["revision"], "fields": clean,
                                     "context": json.loads(json.dumps(self.contexts.get(identifier, {"version": "unavailable", "selected": [], "excluded": []})))})

    def audit(self, token, fields):
        with self.lock: identifier = self.by_token.get(token)
        if identifier: self.decision(identifier, fields)

    def voice_correction(self, text, *, current=None, allow_remember=False):
        remember = re.fullmatch(r"\s*(?:Батлер[,!]?\s+)?запомни\s*[:,]?\s+(.+)", text, re.I)
        if remember:
            if not allow_remember: return None
            # Explicit remember is authorization, not a reason for another
            # confirmation. Persistence is still asynchronous and truthful.
            args = {"action": "remember", "text": remember[1], "title": remember[1][:100],
                    "event": uuid.uuid4().hex, "source": "owner"}
            self._queue("rule", args, priority=0)
            return {"received": True, "saved": False, "applied": False, "message": "Предпочтение принято; сохраняю."}
        kind = next((kind for pattern, kind in VOICE_CUES if re.search(pattern, text, re.I)), None)
        if kind is None: return None
        with self.lock:
            eligible = [r for r in self.rows if r["id"] != current and 0 <= self.service.clock() - r["at"] <= 120]
            voice = self.service.voice
            in_progress = getattr(voice, "reply_in_progress", False) is True or getattr(voice, "turn_committed", False) is True
            active = self.by_token.get(getattr(voice, "token", None)) if in_progress else None
            targets = [r for r in eligible if r["id"] == active]
            if not targets and len(eligible) == 1: targets = eligible
        return self.correct({"event": uuid.uuid4().hex, "kind": kind,
                             "id": targets[0]["id"] if len(targets) == 1 else None,
                             "revision": targets[0]["revision"] if len(targets) == 1 else None,
                             "binding": "active-turn" if active and targets else "unique-episode" if targets else "ambiguous"})

    def correct(self, args):
        s = self.service
        if not s.gate.enabled or not s.voice_gate.permits(s.clock()):
            raise ValueError("correction needs the owner's dialogue session")
        kind, event = args.get("kind"), args.get("event")
        if kind not in KINDS or not isinstance(event, str) or not 1 <= len(event) <= 128:
            raise ValueError("invalid correction")
        self._expire_memory()
        with self.lock:
            request_hash = hashlib.sha256(json.dumps([args.get(k) for k in ("id", "revision", "kind", "text", "annotation", "binding", "memory")], sort_keys=True).encode()).hexdigest()
            if event in self.events:
                if self.events[event].get("request_hash") != request_hash: raise ValueError("correction event reused with different content")
                old = self.events[event]
                original = next((r for r in self.rows if r["id"] == old.get("episode")), None)
                if not old.get("saved") and original is not None:
                    self._queue("correction", {"annotation": dict(old), "corrected": args.get("text"),
                                "retract": args.get("annotation"), "born": original["at"]}, priority=0)
                return dict(self.events[event])
            row = next((r for r in self.rows if r["id"] == args.get("id")), None)
            exact = row is not None and args.get("revision") == row["revision"] and row["revision"] > 0
            binding = args.get("binding", "selected-panel-object") if exact else "ambiguous"
            memory = None
            if exact and kind == "stale_memory":
                choices = [m for m in row["memories"] if not args.get("memory") or m["id"] == args["memory"]]
                if len(choices) == 1:
                    memory = choices[0]; binding = "memory:" + memory["id"]
                else: exact, binding = False, "ambiguous"
            annotation = {"id": "correction-" + uuid.uuid4().hex, "event": event, "kind": kind,
                          "episode": row["id"] if exact else None, "revision": args.get("revision") if exact else None,
                          "state": "captured" if exact else "unresolved", "binding": binding,
                          "received": True, "saved": False, "applied": False, "verified": False}
            annotation["request_hash"] = request_hash
            self.events[event] = annotation
            if len(self.events) > 256: self.events.pop(next(iter(self.events)))
            if exact: row["annotations"].append(annotation)
        # Live repair precedes any storage, model or vault work.
        voice = s.voice
        if kind == "not_addressed":
            if voice is not None:
                voice.cancel(clear_context=True, reason="owner-false-address-correction")
            s.attention.ended(s.clock())
            annotation["applied"] = True
        elif exact and kind == "stale_memory" and memory is not None:
            # This is a correction of the selected decision's evidence, not a
            # general lesson or an implicit rewrite of an unrelated vault note.
            s.knowledge.excluded_hashes.add(memory["hash"])
            self.exclusion_origins.setdefault(memory["hash"], set()).add(annotation["id"])
            if voice is not None: voice.cancel(clear_context=False, reason="owner-memory-correction")
            with self.lock:
                context = self.contexts.get(row["id"], {})
                context["memories"] = [r for r in context.get("memories", []) if r["id"] != memory["id"]]
                context.setdefault("excluded", []).append({"id": memory["id"], "hash": memory["hash"], "reason": "owner-memory-correction"})
            self.decision(row["id"], {"reason": "owner-memory-correction", "outcome": "revised"})
            annotation["applied"] = True
            annotation["message"] = "Фрагмент исключён из контекста. Исправлено решение; общий урок не создан."
        elif exact and kind in {"recognition", "missed_address", "unfinished"}:
            text = args.get("text") if kind == "recognition" else row["text"]
            age = s.clock() - row["at"]
            lifetime = 30 * 86400 if row["annotations"] else 86400
            if row["decision"].get("completed_operation"):
                annotation["message"] = "Операция уже выполнена; повторять её не буду."
            elif row["decision"].get("operation"):
                annotation["message"] = "Запрос уже передавался. Его исход нужно проверить; повторять действие не буду."
            elif age >= lifetime or not row["text"]:
                annotation["message"] = "Текст уже недоступен. Повторите, пожалуйста."
            elif kind == "recognition" and (not isinstance(text, str) or not text.strip()):
                if voice is not None: voice.cancel(clear_context=False, reason="owner-recognition-correction")
                self.continuation = {"id": row["id"], "text": "", "until": s.clock() + 15, "epoch": self.epoch}
                annotation["applied"] = True
                annotation["message"] = "Скажите исправленный текст."
            elif kind == "unfinished":
                if voice is not None: voice.cancel(clear_context=False, reason="owner-unfinished-correction")
                self.continuation = {"id": row["id"], "text": text, "until": s.clock() + 15, "epoch": self.epoch}
                annotation["applied"] = True
                annotation["message"] = "Жду продолжения."
            else:
                if voice is not None:
                    voice.cancel(clear_context=False, reason="owner-turn-correction")
                    voice.token = threading.Event()
                    self.attach(voice.token, row["id"])
                    self.forced_tokens.add(voice.token)
                    # No action is directly executed: ordinary turn authorization,
                    # review/confirmation and task idempotency remain in force.
                    voice.jobs.put_nowait((b"", voice.token, s.voice_gate.generation, True,
                                           "owner-panel", time.monotonic(), True, text))
                    annotation["applied"] = True
        elif exact and kind == "retract":
            old = next((a for a in row["annotations"] if a["id"] == args.get("annotation")), None)
            if old:
                old["state"] = "retracted"
                for content_hash, origins in self.exclusion_origins.items():
                    origins.discard(old["id"])
                    if not origins: s.knowledge.excluded_hashes.discard(content_hash)
        payload = {"annotation": dict(annotation), "corrected": args.get("text"), "retract": args.get("annotation"),
                   "born": row["at"] if exact else s.clock()}
        if exact:
            self._queue("correction", payload, priority=0)
        else:
            annotation["message"] = "Уточните, какую реплику исправить; метка обучения не создана."
        if annotation.get("message") and voice is not None and hasattr(voice, "notice"):
            voice.notice(annotation["message"])
        return dict(annotation)

    def join(self, text):
        pending = getattr(self, "continuation", None)
        self.continuation = None
        if pending and pending["epoch"] == self.epoch and self.service.clock() < pending["until"]:
            joined = (pending["text"] + " " + text).strip()
            with self.lock:
                self.next_forced.add(pending["id"])
                row = next((r for r in self.rows if r["id"] == pending["id"]), None)
                if row is not None: row["text"] = joined
            self._queue("joined", {"id": pending["id"], "text": joined, "born": row["at"] if row else self.service.clock()}, priority=1)
            return joined, pending["id"]
        return text, None

    def extend_pending(self, token, text, current):
        """Revise an unsubmitted owner request, preserving the original ID.

        Caller must establish same-owner private pre-dispatch phase. This
        method neither retries an operation nor bypasses addressee checks.
        """
        with self.lock:
            identifier = self.by_token.get(token)
            row = next((r for r in self.rows if r['id'] == identifier), None)
            if (row is None or identifier == current or not row['text'] or
                    self.service.clock()-row['at'] > 60 or row['decision'].get('completed_operation')):
                return text, current
            joined = (row['text'] + ' ' + text).strip()[:12000]
            row['text'] = joined
            self._queue('joined', {'id': identifier, 'text': joined, 'born': row['at']}, priority=1)
            self.decision(current, {'outcome':'joined-continuation','accepted':False})
            return joined, identifier

    def pause(self):
        with self.lock:
            self.export_cancel.set()
            self.export_cancel = threading.Event()
            self.epoch += 1
            self.continuation = None
            self.rows.clear(); self.by_token.clear()
            self.contexts.clear(); self.retry_jobs.clear()
            self.forced_tokens.clear()
            self.next_forced.clear()
            self.agent_requests.clear()
            self.profile = {"version": "paused", "selected": [], "excluded": []}

    def stop(self):
        self.stopped.set()
        if self.thread is not None: self.thread.join(timeout=2)

    def _open(self):
        from butler_core.learning import LearningStore
        from butler_core.rules import RuleStore
        s = self.service
        # Test/service roots are isolated; production deployment explicitly sets
        # the core metadata path, which default_databases backs up without text.
        metadata_path = s.config.get("feedback_metadata", s.store.root / "feedback-metadata.sqlite3")
        self.storage = LearningStore(metadata_path, s.store.root / "private-episodes", clock=s.clock)
        self.rule_store = RuleStore(s.config.get("vault", s.store.root / "vault"),
                                    s.config.get("workspace", s.store.root / "workspace"), clock=s.clock)

    def _refresh_profile(self, force=False):
        if self.rule_store is not None and (force or time.monotonic() - self.last_profile_refresh >= .5):
            self.last_profile_refresh = time.monotonic()
            try:
                if not self._source_allowed():
                    self.rule_store.block_context("vault-source-denied-or-unavailable")
                    self.profile = {"version": "source-unavailable", "selected": [], "excluded": [], "error": "vault-source-unavailable"}
                    return
                published = self.rule_store.publish()
                self.profile = {"version": published["version"], "selected": published["rules"], "excluded": []}
            except (OSError, ValueError) as error:
                self.profile = {**self.profile, "error": type(error).__name__}

    def _source_allowed(self):
        core = getattr(getattr(self.service, "core_link", None), "core", None)
        if core is None: return not self.service.config.get("require_rule_authorization", False)
        try:
            config = core.Client().call("config.active", _socket_timeout=.1).get("content") or {}
            sources = set(config.get("personas", {}).get("butler", {}).get("sources", []))
            sources.update(g["resource"] for g in config.get("grants", []) if g.get("persona") == "butler" and g.get("kind") == "source")
            return "vault" in sources
        except Exception: return False

    def _restore_history(self):
        if self.rows or not self.service.voice_gate.permits(self.service.clock()): return
        self.storage.expire()
        with self.storage.lock, self.lock:
            rows = self.storage.db.execute("SELECT * FROM episodes ORDER BY born DESC LIMIT 80").fetchall()
            for saved in reversed(rows):
                text = self.storage.text(saved["id"])
                revisions = [json.loads(r[0]) for r in self.storage.db.execute("SELECT data FROM decisions WHERE episode=? ORDER BY revision", (saved["id"],))]
                annotations = []
                fragments = self.storage.fragments(saved["id"])
                for item in self.storage.db.execute("SELECT a.*,c.request_hash,c.repaired FROM annotations a LEFT JOIN correction_applications c ON c.event=a.event WHERE a.episode=? ORDER BY a.at", (saved["id"],)):
                    a = {k: item[k] for k in ("id", "event", "kind", "episode", "revision", "state", "binding", "request_hash")}
                    a.update(received=True, saved=True, applied=bool(item["repaired"]), verified=False)
                    annotations.append(a); self.events[a["event"]] = a
                    if a["kind"] == "stale_memory" and a["state"] == "captured" and a["binding"].startswith("memory:"):
                        memory_id = a["binding"].split(":", 1)[1]
                        refs = list(fragments)
                        for receipt in self.storage.db.execute("SELECT data FROM receipts WHERE episode=?", (saved["id"],)):
                            refs.extend(json.loads(receipt[0]).get("memories", []))
                        for reference in refs:
                            if reference["id"] == memory_id:
                                self.service.knowledge.excluded_hashes.add(reference["hash"])
                                self.exclusion_origins.setdefault(reference["hash"], set()).add(a["id"])
                self.rows.append({"id": saved["id"], "turn": saved["turn_id"], "session": saved["session_id"],
                    "at": saved["born"], "mode": saved["scope"], "text": (text["corrected"] or text["original"]) if text else "",
                    "original": text["original"] if text else "", "revision": len(revisions),
                    "decision": revisions[-1] if revisions else {}, "revisions": revisions, "annotations": annotations,
                    "memories": fragments, "saved": True})

    def _expire_memory(self):
        now = self.service.clock()
        with self.lock:
            lifetimes = {r["id"]: (30 if r["annotations"] else 1) * 86400 for r in self.rows}
            for row in self.rows:
                if now - row["at"] >= lifetimes[row["id"]]:
                    row["text"] = row["original"] = ""; row["memories"] = []
            with self.jobs.mutex:
                pending = list(self.jobs.queue) + list(self.retry_jobs)
                for _, _, _, command, payload in pending:
                    identifier = payload.get("id") or payload.get("annotation", {}).get("episode")
                    born = payload.get("at", payload.get("born"))
                    if born is not None and now - born >= lifetimes.get(identifier, 30 * 86400 if command in {"correction", "joined"} else 86400):
                        if "text" in payload: payload["text"] = ""
                        if "original" in payload: payload["original"] = ""
                        if "corrected" in payload: payload["corrected"] = None
                        if "memories" in payload: payload["memories"] = []

    def context(self, token, speech, *, mode, application="", apply=True):
        if self.thread is not None and self.thread.is_alive():
            refreshed = threading.Event()
            self._queue("profile", {"done": refreshed}, priority=1)
            if not refreshed.wait(.03):
                self.profile = {"version": "authorization-pending", "selected": [], "excluded": [], "error": "profile-refresh-deferred"}
        with self.lock:
            profile = self.profile
            selected, excluded = [], []
            for rule in profile["selected"]:
                scope = rule["scope"]
                matches = (scope.get("mode", "private") in {mode, "all"}
                           and (not scope.get("application") or scope["application"] == application)
                           and (not scope.get("contains") or scope["contains"].casefold() in speech.casefold()))
                # General replies never receive free-form owner context. Only
                # these non-personal compiled style instructions can cross it.
                if mode == "general" and rule["text"].strip() not in {"Отвечай кратко.", "Отвечай по-русски.", "Be concise."}:
                    matches = False
                within_budget = sum(len(r["text"]) + 1 for r in selected) + len(rule["text"]) <= 6000
                if matches and apply and within_budget: selected.append(rule)
                else: excluded.append({"id": rule["id"], "revision": rule["revision"],
                                       "reason": "not-used-by-deterministic-rule" if matches and not apply else "context-budget" if matches else "scope"})
            context = {"version": profile["version"], "selected": selected, "excluded": excluded}
            identifier = self.by_token.get(token)
            if identifier:
                old = self.contexts.get(identifier, {})
                context.update({k: old[k] for k in ("memories", "export") if k in old})
                self.contexts[identifier] = context
        return "\n".join(rule["text"] for rule in selected)[:6000]

    def register_agent(self, token, speech, application):
        nonce = uuid.uuid4().hex
        with self.lock:
            self.agent_requests[nonce] = {"episode": self.by_token.get(token), "speech": speech, "application": application,
                "generation": self.service.voice_gate.generation, "epoch": self.epoch, "cancel": self.export_cancel,
                "expires": self.service.clock() + 300, "read": False}
            if len(self.agent_requests) > 16: self.agent_requests.pop(next(iter(self.agent_requests)))
        return nonce

    def agent_context(self, nonce):
        """DEC-075: scope-aware reload through the existing butler-lesson tool."""
        with self.lock: request = self.agent_requests.get(nonce)
        s = self.service
        def permitted():
            return (request is not None and request["epoch"] == self.epoch and not request["cancel"].is_set()
                    and s.clock() < request["expires"] and s.gate.enabled and s.voice_gate.permits(s.clock())
                    and s.voice_gate.generation == request["generation"])
        if not permitted() or not self._source_allowed(): raise ValueError("rule context unavailable")
        from butler_core.rules import RuleStore
        rules = RuleStore(s.config.get("vault", s.store.root / "vault"), s.config.get("workspace", s.store.root / "workspace"), clock=s.clock)
        profile = rules.matching(mode="private", application=request["application"], speech=request["speech"])
        selected, excluded, used = [], list(profile["excluded"]), 0
        for rule in profile["selected"]:
            if rule["kind"] not in {"lesson", "preference"}: continue
            if used + len(rule["text"]) > 6000:
                excluded.append({"id": rule["id"], "revision": rule["revision"], "reason": "context-budget"}); continue
            used += len(rule["text"]); selected.append(rule)
        if not permitted(): raise ValueError("rule context permission changed")
        context = {"version": profile["version"], "selected": selected, "excluded": excluded, "export": "owner-addressed"}
        receipt = None
        with self.lock:
            self.contexts[request["episode"]] = {**self.contexts.get(request["episode"], {}), **context}
            row = next((r for r in self.rows if r["id"] == request["episode"]), None)
        if self.storage is not None and row is not None:
            try: receipt = self.storage.receipt(row["id"], row["revision"], profile["version"], rules=selected, excluded=excluded, export="owner-addressed")
            except Exception as error: self.error = type(error).__name__
        request["read"] = True
        return {"profile": profile["version"], "rules": [{k: r[k] for k in ("id", "revision", "hash", "text", "scope")} for r in selected],
                "receipt_saved": receipt is not None, "authority": "advisory rules only; no additional permissions", "error": profile.get("error")}

    def agent_read(self, nonce):
        with self.lock: return bool(self.agent_requests.get(nonce, {}).get("read"))

    def memory_context(self, token, memories, *, export="local"):
        with self.lock:
            identifier = self.by_token.get(token)
            if identifier is None: return
            context = self.contexts.setdefault(identifier, {"version": self.profile["version"], "selected": [], "excluded": []})
            context["memories"] = [{"id": hashlib.sha256(json.dumps([m.get("scope"), m.get("path"), m.get("line_no")]).encode()).hexdigest(),
                                    "hash": hashlib.sha256(m.get("snippet", "").encode()).hexdigest(), "reason": "local-recall"} for m in memories]
            context["export"] = export
            row = next((r for r in self.rows if r["id"] == identifier), None)
            if row is not None:
                row["memories"] = [{**m, **reference} for m, reference in zip(memories, context["memories"])]
                self._queue("memories", {"id": identifier, "memories": list(row["memories"]), "born": row["at"]})

    def rule_control(self, args):
        s = self.service
        if not s.gate.enabled or not s.voice_gate.permits(s.clock()): raise ValueError("owner session required")
        from butler_core.rules import RuleStore
        rules = RuleStore(s.config.get("vault", s.store.root / "vault"), s.config.get("workspace", s.store.root / "workspace"), clock=s.clock)
        action = args.get("action")
        if action == "list":
            if not self._source_allowed(): raise ValueError("vault source unavailable")
            rows = list(rules.scan().values())
            for row in rows:
                row["uses"] = self.storage.rule_uses(row["id"]) if self.storage is not None else []
            return {"rules": rows, "profile": rules.profile()["version"]}
        operation = args.get("event")
        if not isinstance(operation, str) or not operation: raise ValueError("operation identity required")
        allowed = self._source_allowed()
        if not allowed and action not in {"retire", "delete"}: raise ValueError("vault source unavailable")
        if action == "remember":
            identifier = args.get("id") or "preference-" + hashlib.sha256(operation.encode()).hexdigest()[:24]
            result = rules.put(identifier, args.get("title") or args["text"][:100], args["text"], operation=operation,
                               authorized=True, scope=args.get("scope"), kind="preference")
        elif action in {"edit", "narrow", "retire", "delete"}:
            result = rules.change(args["id"], operation=operation, authorized=True, text=args.get("text"),
                                  scope=args.get("scope"), status="retired" if action == "retire" else None, delete=action == "delete")
        else: raise ValueError("unknown rule action")
        published = rules.profile() if allowed else rules.block_context("vault-source-unavailable")
        with self.lock:
            self.profile = {"version": published["version"], "selected": published["rules"], "excluded": []}
            self.receipts.append(result)
        return result

    def _loop(self):
        while not self.stopped.is_set():
            try: priority, serial, epoch, command, payload = self.jobs.get(timeout=.2)
            except queue.Empty:
                try:
                    self._expire_memory()
                    if self.storage is not None: self.storage.expire()
                    if self.service.gate.enabled:
                        if self.storage is None: self._open()
                        self._refresh_profile()
                        self.storage.expire()
                        self._restore_history()
                        if self.retry_jobs and time.monotonic() >= self.retry_at:
                            self.jobs.put_nowait(self.retry_jobs.popleft())
                            self.retry_at = time.monotonic() + 1
                except Exception as error:
                    self.failed += 1; self.error = type(error).__name__
                continue
            try:
                if self.storage is None: self._open()
                if command == "attention":
                    with self.storage.lock:
                        self.storage.db.execute("INSERT OR IGNORE INTO attention VALUES(?,?,?)", (payload["at"], payload["state"], payload["cause"]))
                        self.storage.db.commit()
                    continue
                if epoch != self.epoch or not self.service.gate.enabled: continue
                if command == "rule":
                    self.rule_control(payload)
                elif command == "profile":
                    try: self._refresh_profile(force=True)
                    finally: payload["done"].set()
                elif command == "joined":
                    with self.storage.lock:
                        self.storage.private.execute("UPDATE texts SET corrected=? WHERE id=? AND expires>?", (payload["text"], payload["id"], self.service.clock()))
                        self.storage.private.commit()
                elif command == "memories":
                    self.storage.save_fragments(payload["id"], payload["memories"])
                elif command == "episode":
                    self.storage.episode(payload["id"], payload["turn"], payload["session"], payload["at"], payload["text"], scope=payload["mode"])
                elif command == "transcript":
                    self.storage.transcript(payload['id'], payload['revision'], payload['text'],
                                            born=payload['born'], state=payload['state'])
                elif command == "decision":
                    revision = self.storage.decide(payload["id"], payload["fields"], revision=payload["revision"])
                    profile = payload["context"]
                    self.storage.receipt(payload["id"], revision, profile["version"], rules=profile["selected"],
                                         excluded=profile.get("excluded", []), memories=profile.get("memories", []), export=profile.get("export", "local"))
                elif command == "correction":
                    a = payload["annotation"]
                    # Corrections have priority; ensure their referenced episode
                    # and revision exist even when capture work is still queued.
                    with self.lock: row = next((dict(r) for r in self.rows if r["id"] == a["episode"]), None)
                    if row:
                        self.storage.episode(row["id"], row["turn"], row["session"], row["at"], row["original"], scope=row["mode"])
                        for n, decision in enumerate(row["revisions"][:a["revision"]], 1):
                            self.storage.decide(row["id"], decision, revision=n)
                    stored = self.storage.annotate(a["event"], a["episode"], a["revision"], a["kind"], authorized=True,
                        binding=a["binding"], corrected=payload["corrected"], retract=payload["retract"], identifier=a["id"],
                        repaired=a["applied"], client_hash=a["request_hash"])
                    with self.lock:
                        if a["event"] in self.events: self.events[a["event"]]["saved"] = stored["state"] != "unresolved"
                self.storage.expire()
                self.report = self.storage.report()
                self.error = None
            except Exception as error:
                self.failed += 1; self.error = type(error).__name__
                if len(self.retry_jobs) < 256:
                    self.retry_jobs.append((priority, serial, epoch, command, payload))
                self.retry_at = time.monotonic() + 1
                # No exception escapes into hearing, playback or immediate repair.
            finally: self.jobs.task_done()
        if self.storage is not None: self.storage.close()

    def snapshot(self, allowed):
        with self.lock:
            now = self.service.clock()
            for row in self.rows:
                if now - row["at"] >= (30 if row["annotations"] else 1) * 86400:
                    row["text"] = row["original"] = ""
                    row["memories"] = []
            rows = [{**{k: v for k, v in r.items() if k not in {"text", "original", "decision", "session", "revisions"}},
                     "text": r["text"], "decision": r["decision"]} for r in self.rows] if allowed else []
            return {"episodes": rows, "backlog": self.jobs.qsize() + len(self.retry_jobs), "failed": self.failed, "error": self.error or self.profile.get("error"),
                    "weekly": self.report if allowed else None, "rules": self.profile if allowed else None,
                    "write_receipts": list(self.receipts) if allowed else [], "audio_examples": False}
