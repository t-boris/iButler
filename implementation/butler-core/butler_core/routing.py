"""Routing: rules, then System 1 in shadow, then System 2 (REQ-021, REQ-024).

Rules answer explicit owner commands (task status, explicit continuation,
approvals and resolutions) without a model. System 1 only records what it
would have decided; it never changes execution while in shadow mode. Every
other message goes to System 2 (the persona's OpenClaw agent). Whatever acts
on the outside world, from any of the three, goes through ``Operations.submit``.
"""
from __future__ import annotations

import re
import threading
import time

from .approvals import ApprovalError
from .operations import OperationError
from .store import new_id
from .tasks import TaskError

COMMAND = re.compile(r"^/(tasks|task|continue|approve|reject|resolve)(?:_([0-9a-f]{6,16}))?(?:_([0-9a-z]{1,10}))?"
                     r"(?:@\w+)?(?:\s+(.*))?$", re.S)
STATE_RU = {"accepted": "принята", "scheduled": "запланирована", "awaiting_approval": "ждёт подтверждения",
            "running": "выполняется", "succeeded": "выполнена", "failed": "не выполнена", "cancelled": "отменена",
            "needs_reconciliation": "результат неизвестен — нужна ваша проверка", "interrupted": "прервана"}


def short(identifier):
    return identifier.split("-", 1)[-1][:8]


class Rules:
    def __init__(self, core):
        self.core = core

    def match(self, text):
        return COMMAND.match(text.strip()) if isinstance(text, str) else None

    def _task_line(self, task):
        goal = " ".join(task["goal"].split())
        return f"• {short(task['id'])} — {STATE_RU.get(task['state'], task['state'])}: {goal[:80]}"

    def _disclose(self, persona, principal, task_id, correlation):
        decision = self.core.policy.check(persona, "disclose", f"task:{task_id}", principal=principal,
                                          correlation=correlation)
        return decision["outcome"] == "allow"

    def _operation(self, prefix):
        rows = self.core.store.query("SELECT id FROM operations WHERE id LIKE ? LIMIT 2", (f"op-{prefix}%",))
        if len(rows) != 1:
            raise OperationError("операция не найдена" if not rows else "неоднозначный номер операции")
        return rows[0][0]

    def handle(self, match, *, persona, principal, channel, correlation):
        """Return the reply text for one recognized command."""
        command, ident, extra, rest = match.groups()
        try:
            if command == "tasks":
                rows = [t for t in self.core.tasks.list(persona=persona, limit=10, kinds=("chat", "operation"))
                        if self._disclose(persona, principal, t["id"], correlation)]
                if not rows:
                    return "Задач пока нет."
                return "Последние задачи:\n" + "\n".join(self._task_line(t) for t in rows)
            if command in ("task", "continue"):
                if not ident:
                    return "Укажите номер задачи, например /task_1a2b3c4d."
                task_id = self.core.tasks.resolve_id(ident)
                task = self.core.tasks.get(task_id, history=False)
                # Authorize before anything about the task is revealed.
                if not self._disclose(persona, principal, task_id, correlation):
                    return "Эта задача недоступна в этом канале."
                if command == "task":
                    return self.core.describe_task(task)
                return self.core.continue_task(task_id, persona=persona, principal=principal, channel=channel,
                                               correlation=correlation)
            if command in ("approve", "reject", "resolve"):
                if not ident:
                    return "Укажите номер операции."
                op_id = self._operation(ident)
                if command == "approve":
                    if not extra:
                        return "Нужен код проверки из запроса подтверждения."
                    self.core.approvals.grant(op_id, extra, principal, channel)
                    self.core.wake()
                    return f"Подтверждено: операция {short(op_id)} будет выполнена один раз."
                if command == "reject":
                    self.core.reject(op_id, principal, channel)
                    return f"Отклонено: операция {short(op_id)} не будет выполнена."
                outcome = {"done": "done", "notdone": "not_done"}.get(extra or "")
                if outcome is None or not rest:
                    return "Формат: /resolve_<номер>_done <что проверено> или /resolve_<номер>_notdone <что проверено>."
                self.core.resolve(op_id, outcome, rest, principal, channel)
                return f"Записано: операция {short(op_id)} — {'выполнена' if outcome == 'done' else 'не выполнена'}." \
                       " Повторного выполнения не будет."
        except (TaskError, OperationError, ApprovalError) as error:
            return f"Не выполнено: {error}"
        return "Неизвестная команда."


class NullModel:
    name = "none"

    def available(self):
        return False

    def predict(self, text):
        raise RuntimeError("no System 1 model installed")


class Shadow:
    """Record System 1 predictions next to actual decisions; never act."""

    def __init__(self, store, config, model=None, clock=time.time):
        self.store, self.config, self.model, self.clock = store, config, model or NullModel(), clock
        self.threads = []

    def mode(self):
        _, content = self.config.active()
        return (content or {}).get("routing", {}).get("system1", {}).get("mode", "shadow")

    def observe(self, event_id, text, actual, background=True):
        if self.mode() == "off" or not self.model.available() or not isinstance(text, str):
            return None

        def run():
            started = time.perf_counter()
            try:
                label, confidence = self.model.predict(text)
            except Exception:
                return
            latency = (time.perf_counter() - started) * 1000
            try:
                with self.store.transaction(guard=False) as db:
                    db.execute("INSERT INTO shadow VALUES(?,?,?,?,?,?,?,?)",
                               (new_id("s1"), self.clock(), event_id, str(label)[:100], actual, float(confidence),
                                self.model.name, latency))
            except OSError:
                pass

        if not background:
            run()
            return None
        thread = threading.Thread(target=run, name="system1-shadow", daemon=True)
        thread.start()
        self.threads = [t for t in self.threads if t.is_alive()] + [thread]
        return thread

    def report(self):
        rows = self.store.query("SELECT predicted, actual, COUNT(*) n, AVG(latency_ms) ms FROM shadow"
                                " GROUP BY predicted, actual")
        return [dict(r) for r in rows]
