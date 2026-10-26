"""System 3: slow learning from the owner's signals (REQ-027, DEC-058, DEC-059).

The core already records every owner signal as a feedback event with the
Butler actions of the preceding window. This module, on a slow schedule and
never during a Full Pause, asks the local model why the owner reacted, keeps
the answer as a lesson candidate with its provenance and confidence, asks the
owner a short question when the cause is unclear (bounded per week, outside
quiet hours), exports confirmed lessons into the persona's existing lesson
store (``butler-lesson``) so later turns load them, and checks that confirmed
lessons are followed. It never acts on the outside world: a proposal to change
limits is a configuration proposal the owner activates.

Only metadata reaches the model: signal kind, counts and kinds of the
preceding deliveries and operations, time of day. The raw context stays local
and is referenced by id (DEC-059); the de-identified summaries are kept for
System 2 clustering as ``feedback-summaries.jsonl`` under the core's state.
"""
from __future__ import annotations

import datetime as dt
import http.client
import json
import subprocess
import time
from pathlib import Path
from zoneinfo import ZoneInfo

from .store import dumps, loads, new_id

CAUSES = ("too_intrusive", "wrong_place", "too_many_questions", "poor_questions", "wrong_timing", "unwanted_content", "other")
CAUSE_TEXT = {"too_intrusive": "Butler вмешивался слишком часто или не вовремя",
              "wrong_place": "Butler отправил или сказал что-то не туда и не тому",
              "too_many_questions": "Butler задавал слишком много вопросов",
              "poor_questions": "Butler задавал неудачные вопросы",
              "wrong_timing": "Butler выбрал неудачный момент",
              "unwanted_content": "Butler говорил или присылал нежелательное",
              "other": "причина пока неясна"}
SIGNALS = {"full_pause": "full_pause", "pause": "pause", "mute": "mute", "cancel": "cancel", "approval_rejected": "approval_rejected",
           "correction": "correction", "not_addressed": "not_addressed", "complaint": "complaint", "silence_request": "silence_request",
           "consent_revoked": "consent_revoked", "ignored_initiative": "ignored_initiative"}
ANALYSIS_INTERVAL = 6 * 3600
COMPLIANCE_INTERVAL = 24 * 3600
QUESTIONS_PER_WEEK = 2
VIOLATION_ALARM = 3
OLLAMA = ("127.0.0.1", 11434)
SCHEMA = {"type": "object", "required": ["cause", "confidence", "lesson"],
          "properties": {"cause": {"type": "string", "enum": list(CAUSES)}, "confidence": {"type": "number"},
                         "lesson": {"type": "string"}}}
PROMPT = """Ты анализируешь, почему владелец (Борис) дал сигнал ассистенту Butler. Ответь JSON по схеме.
Причины: too_intrusive (слишком часто/навязчиво), wrong_place (не туда/не тому), too_many_questions, poor_questions,
wrong_timing (не вовремя: ночь, встреча, занят), unwanted_content (нежелательное содержание), other (неясно).
confidence — от 0 до 1. lesson — одно короткое правило для Butler по-русски, без имён и цитат.

Сигнал: {kind} ({detail})
Время: {hour}:00 по Чикаго, {weekday}
За предыдущие {window_min} минут Butler: отправил сообщений {deliveries} ({delivery_kinds}); выполнил операций {operations} ({operation_kinds}).
Повторы такого сигнала за 7 дней: {repeats}.
"""


def local_model(prompt, model="qwen3.5:9b", timeout=120):
    body = json.dumps({"model": model, "prompt": prompt, "stream": False, "think": False, "format": SCHEMA,
                       "keep_alive": "5m", "options": {"temperature": 0, "num_ctx": 4096}}).encode()
    conn = http.client.HTTPConnection(*OLLAMA, timeout=timeout)
    try:
        conn.request("POST", "/api/generate", body, {"Content-Type": "application/json"})
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8", "replace"))
        if response.status != 200:
            raise RuntimeError(f"local model answered {response.status}")
        verdict = json.loads(payload.get("response", ""))
    finally:
        conn.close()
    cause = verdict.get("cause") if verdict.get("cause") in CAUSES else "other"
    confidence = float(verdict.get("confidence") or 0)
    return {"cause": cause, "confidence": max(0.0, min(1.0, confidence)), "lesson": str(verdict.get("lesson") or "")[:300]}


class System3:
    def __init__(self, core, *, model=local_model, workspace=None, lesson_cli=None, clock=time.time, zone="America/Chicago"):
        self.core, self.model, self.clock = core, model, clock
        self.workspace = Path(workspace) if workspace else Path.home() / ".openclaw/workspace-butler"
        self.lesson_cli = Path(lesson_cli) if lesson_cli else self.workspace / "bin/butler-lesson"
        self.zone = ZoneInfo(zone)
        self.summaries = Path(core.root) / "feedback-summaries.jsonl"

    # -- schedule -----------------------------------------------------------------------

    def settings(self):
        _, content = self.core.config.active()
        return (content or {}).get("system3", {})

    def enabled(self):
        _, content = self.core.config.active()
        return bool((content or {}).get("scenarios", {}).get("system3", {}).get("enabled"))

    def maybe_run(self):
        if not self.enabled():
            return None
        now = self.clock()
        result = {}
        last = self.core.store.meta("system3_analysed")
        if last is None or now - float(last) >= self.settings().get("analysis_interval", ANALYSIS_INTERVAL):
            result["analysis"] = self.analyse(now)
            self.core.store.set_meta("system3_analysed", now)
        last = self.core.store.meta("system3_checked")
        if last is None or now - float(last) >= COMPLIANCE_INTERVAL:
            result["compliance"] = self.compliance(now)
            self.core.store.set_meta("system3_checked", now)
        return result

    # -- analysis -------------------------------------------------------------------------

    def full_pause(self, persona, now):
        """A Full Pause is on while the newest pause-class signal has no later resume."""
        rows = self.core.feedback.list(since=now - 7 * 86400, kinds=["full_pause", "resume"], persona=persona, limit=1)
        return bool(rows) and rows[0]["kind"] == "full_pause"

    def summarize(self, signal, now):
        """De-identified: what kind of signal, when, what Butler had just done — counts and kinds only."""
        context = signal.get("context") or {}
        deliveries = context.get("deliveries") or []
        operations = context.get("operations") or []
        local = dt.datetime.fromtimestamp(signal["at"], self.zone)
        repeats = len(self.core.feedback.list(since=signal["at"] - 7 * 86400, kinds=[signal["kind"]], persona=signal["persona"], limit=100))
        return {"feedback": signal["id"], "kind": signal["kind"], "persona": signal["persona"], "at": signal["at"],
                "hour": local.hour, "weekday": local.strftime("%A"), "window_min": 30,
                "detail": {k: v for k, v in (signal.get("detail") or {}).items()},
                "deliveries": len(deliveries), "delivery_kinds": sorted({d.get("kind") for d in deliveries if d.get("kind")}),
                "operations": len(operations), "operation_kinds": sorted({o.get("kind") for o in operations if o.get("kind")}),
                "repeats": repeats}

    def analyse(self, now=None, limit=50):
        now = self.clock() if now is None else now
        rows = self.core.feedback.list(unanalysed=True, limit=limit)
        others = [s["id"] for s in rows if s["kind"] not in SIGNALS]
        if others:
            # Resumes, resolutions and answers are evidence for other signals, not signals to explain.
            with self.core.store.transaction(guard=False) as db:
                db.executemany("UPDATE feedback SET analysed_at=? WHERE id=?", [(now, i) for i in others])
        pending = [s for s in rows if s["kind"] in SIGNALS]
        done, skipped, failed = 0, 0, 0
        for signal in reversed(pending):
            if self.full_pause(signal["persona"], now):
                skipped += 1
                continue
            summary = self.summarize(signal, now)
            prompt = PROMPT.format(kind=summary["kind"], detail=", ".join(f"{k}={v}" for k, v in summary["detail"].items()) or "—",
                                   hour=summary["hour"], weekday=summary["weekday"], window_min=summary["window_min"],
                                   deliveries=summary["deliveries"], delivery_kinds=", ".join(summary["delivery_kinds"]) or "—",
                                   operations=summary["operations"], operation_kinds=", ".join(summary["operation_kinds"]) or "—",
                                   repeats=summary["repeats"])
            try:
                verdict = self.model(prompt)
            except Exception as error:
                failed += 1
                self.core.diagnostics.raise_alarm("system3:model", f"lesson analysis unavailable: {type(error).__name__}")
                break
            self.core.diagnostics.clear_alarm("system3:model")
            lesson_id = self._record(signal, summary, verdict, now)
            with self.core.store.transaction(guard=False) as db:
                db.execute("UPDATE feedback SET analysed_at=? WHERE id=?", (now, signal["id"]))
            with self.summaries.open("a") as stream:
                stream.write(json.dumps({**summary, "cause": verdict["cause"], "confidence": verdict["confidence"], "lesson": lesson_id},
                                        ensure_ascii=False) + "\n")
            done += 1
        asked = self.ask_if_unclear(now) if done else 0
        return {"analysed": done, "skipped_full_pause": skipped, "failed": failed, "asked": asked}

    def _record(self, signal, summary, verdict, now):
        """Merge into the open candidate for the same persona, cause and signal kind, or open a new one."""
        with self.core.store.transaction(guard=False) as db:
            row = db.execute("SELECT * FROM lessons WHERE persona=? AND cause=? AND signal=? AND status IN ('candidate','confirmed')"
                             " ORDER BY created_at DESC LIMIT 1", (signal["persona"], verdict["cause"], signal["kind"])).fetchone()
            if row is not None:
                sources = loads(row["sources"], [])
                sources.append(signal["id"])
                confidence = round((row["confidence"] * (len(sources) - 1) + verdict["confidence"]) / len(sources), 3)
                db.execute("UPDATE lessons SET sources=?, confidence=?, updated_at=?, text=COALESCE(NULLIF(text,''),?) WHERE id=?",
                           (dumps(sources[-50:]), confidence, now, verdict["lesson"], row["id"]))
                return row["id"]
            lesson_id = new_id("ls")
            title = f"{CAUSE_TEXT.get(verdict['cause'], verdict['cause'])} · сигнал {signal['kind']}"
            db.execute("INSERT INTO lessons(id, persona, cause, signal, title, text, confidence, status, created_at, updated_at, sources)"
                       " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (lesson_id, signal["persona"], verdict["cause"], signal["kind"], title, verdict["lesson"],
                        round(verdict["confidence"], 3), "candidate", now, now, dumps([signal["id"]])))
            return lesson_id

    # -- owner questions ------------------------------------------------------------------

    def ask_if_unclear(self, now):
        limit = int(self.settings().get("questions_per_week", QUESTIONS_PER_WEEK))
        asked = self.core.store.query("SELECT COUNT(*) FROM deliveries WHERE kind='question' AND created_at>=?", (now - 7 * 86400,))[0][0]
        count = 0
        for row in self.core.store.query("SELECT * FROM lessons WHERE status='candidate' AND confidence<0.5 AND asked_at IS NULL"
                                         " ORDER BY updated_at DESC LIMIT 5"):
            if asked + count >= limit:
                break
            if self.full_pause(row["persona"], now):
                continue
            text = (f"Вопрос, чтобы учиться на обратной связи: недавно был сигнал «{row['signal']}», я предположил «{CAUSE_TEXT.get(row['cause'])}», "
                    f"но не уверен. Что было не так? Ответь одной фразой — или «неважно». (урок {row['id'][-6:]})")
            delivered = self.core.notify(row["persona"], text, "question", f"system3:question:{row['id']}", correlation=row["id"])
            if delivered is None or delivered.get("state") == "denied":
                continue
            with self.core.store.transaction(guard=False) as db:
                db.execute("UPDATE lessons SET asked_at=? WHERE id=?", (now, row["id"]))
            count += 1
        return count

    def answer(self, lesson_id, text, actor):
        """The owner's answer is evidence of that lesson; it raises confidence and may name the cause."""
        row = self._lesson(lesson_id)
        lowered = str(text).lower()
        cause = next((c for c in CAUSES if c in lowered), row["cause"])
        if "неважно" in lowered or "не важно" in lowered:
            return self.set_status(lesson_id, "retired", actor)
        with self.core.store.transaction(guard=False) as db:
            db.execute("UPDATE lessons SET cause=?, confidence=?, text=?, updated_at=?, answer=? WHERE id=?",
                       (cause, max(row["confidence"], 0.8), str(text)[:300] if len(str(text)) > 8 else row["text"], self.clock(),
                        str(text)[:300], row["id"]))
            self.core.feedback.record("request", persona=row["persona"], principal=actor, subject=row["id"], db=db,
                                      detail={"source": "lesson-answer"})
        return self.get(lesson_id)

    # -- lessons ----------------------------------------------------------------------------

    def _lesson(self, lesson_id):
        row = self.core.store.one("SELECT * FROM lessons WHERE id=? OR id LIKE ?", (lesson_id, f"%{lesson_id}"))
        if row is None:
            raise KeyError(f"no lesson {lesson_id}")
        return row

    def get(self, lesson_id):
        return self.public(self._lesson(lesson_id))

    @staticmethod
    def public(row):
        return {"id": row["id"], "persona": row["persona"], "cause": row["cause"], "signal": row["signal"], "title": row["title"],
                "text": row["text"], "confidence": row["confidence"], "status": row["status"], "sources": loads(row["sources"], []),
                "violations": row["violations"], "created_at": row["created_at"], "updated_at": row["updated_at"],
                "exported_id": row["exported_id"], "asked_at": row["asked_at"], "answer": row["answer"]}

    def list(self, status=None, persona=None, limit=50):
        sql, params = "SELECT * FROM lessons", []
        clauses = []
        if status:
            clauses.append("status=?"); params.append(status)
        if persona:
            clauses.append("persona=?"); params.append(persona)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        return [self.public(r) for r in self.core.store.query(sql, params)]

    def set_status(self, lesson_id, status, actor, text=None):
        if status not in ("candidate", "confirmed", "retired", "deleted"):
            raise ValueError("status must be candidate, confirmed, retired or deleted")
        row = self._lesson(lesson_id)
        now = self.clock()
        exported = row["exported_id"]
        if status == "confirmed" and not exported:
            exported = self.export(row, text or row["text"])
        if status in ("retired", "deleted") and exported:
            self.unexport(exported)
        with self.core.store.transaction(guard=False) as db:
            if status == "deleted":
                db.execute("DELETE FROM lessons WHERE id=?", (row["id"],))
            else:
                db.execute("UPDATE lessons SET status=?, text=COALESCE(?, text), updated_at=?, exported_id=?, confirmed_at=COALESCE(confirmed_at, ?)"
                           " WHERE id=?", (status, text, now, exported if status == "confirmed" else None,
                                           now if status == "confirmed" else None, row["id"]))
            self.core.feedback.record("resolved", persona=row["persona"], principal=actor, subject=row["id"], db=db,
                                      detail={"outcome": status})
        return None if status == "deleted" else self.get(row["id"])

    def export(self, row, text):
        """Confirmed lessons live in the persona's existing store, which the agent bootstrap loads (DEC-058)."""
        day = dt.datetime.fromtimestamp(self.clock(), self.zone).date().isoformat()
        lesson_id = f"L-{day}-{int(self.clock()) % 1000}"
        body = (f"**System 3** · причина: {CAUSE_TEXT.get(row['cause'], row['cause'])} · сигнал: {row['signal']} · "
                f"уверенность {row['confidence']:.2f} · источники: {len(loads(row['sources'], []))} событий.\n\n**Правило**: {text}\n")
        done = subprocess.run([str(self.lesson_cli), "add", lesson_id, f"[System 3] {row['title']}"], input=body, capture_output=True,
                              text=True, timeout=60, cwd=str(self.workspace))
        if done.returncode:
            raise RuntimeError(f"lesson export failed: {(done.stderr or done.stdout).strip()[-200:]}")
        return lesson_id

    def unexport(self, exported_id):
        subprocess.run([str(self.lesson_cli), "retire", exported_id], capture_output=True, text=True, timeout=60, cwd=str(self.workspace))

    # -- compliance ---------------------------------------------------------------------------

    def compliance(self, now=None):
        """For every confirmed lesson: the same signal kind after confirmation is a violation."""
        now = self.clock() if now is None else now
        report = []
        for row in self.core.store.query("SELECT * FROM lessons WHERE status='confirmed'"):
            since = row["confirmed_at"] or row["updated_at"]
            violations = len([f for f in self.core.feedback.list(since=since, kinds=[row["signal"]], persona=row["persona"], limit=500)
                              if f["at"] > since])
            with self.core.store.transaction(guard=False) as db:
                db.execute("UPDATE lessons SET violations=?, last_check=? WHERE id=?", (violations, now, row["id"]))
            key = f"system3:lesson:{row['id'][-6:]}"
            if violations >= VIOLATION_ALARM:
                self.core.diagnostics.raise_alarm(key, f"lesson {row['id'][-6:]} violated {violations} times since confirmation")
                self.core.notify(row["persona"], f"⚠️ Урок «{row['title']}» нарушен {violations} раз с подтверждения. "
                                 f"Проверь правило: butler-core lessons list", "status", f"system3:violated:{row['id']}:{int(now // 86400)}")
            else:
                self.core.diagnostics.clear_alarm(key)
            report.append({"lesson": row["id"], "violations": violations})
        return report

    def snapshot(self):
        counts = {r[0]: r[1] for r in self.core.store.query("SELECT status, COUNT(*) FROM lessons GROUP BY status")}
        return {"enabled": self.enabled(), "lessons": counts, "unanalysed": len(self.core.feedback.list(unanalysed=True, limit=500)),
                "last_analysis": self.core.store.meta("system3_analysed"), "last_check": self.core.store.meta("system3_checked")}
