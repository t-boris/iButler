"""What needs the owner: one short message a day (BUG-021, BUG-022).

Things the core cannot settle by itself used to sit in its database unseen. This module gathers them into a single
``digest`` delivery, sent at most once per local day and never during quiet hours:

* operations that wait for the owner's approval, oldest first, with the commands that answer them (BUG-021);
* operations that waited too long and were expired without running (BUG-021);
* deliveries whose outcome nobody could confirm (they are never resent, see ``delivery``) (BUG-022).

Nothing is sent when nothing needs attention. Waiting approvals are listed every day until answered or expired; expired
operations and uncertain deliveries are listed until a digest that carried them was delivered.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from .approvals import review_code
from .routing import short

KEY_PREFIX = "attention:"
FIRST_LOOKBACK = 3 * 86400
LIST_LIMIT = 10
PREVIEW = 80
MONTHS = ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "нояб", "дек")
# "imported" rows are history copied from the gateway's failed queue, not something that happened since the last digest.
# Approval requests have their own single resend and their own section.
NOT_LISTED = ("approval", "imported")


def stamp(moment, zone):
    local = dt.datetime.fromtimestamp(moment, ZoneInfo(zone))
    return f"{local.day} {MONTHS[local.month - 1]} {local:%H:%M}"


class Attention:
    def __init__(self, core):
        self.core = core

    def _since(self, now):
        """Entries newer than the last digest the owner actually received."""
        row = self.core.store.one("SELECT MAX(created_at) FROM deliveries WHERE idempotency_key LIKE ?"
                                  " AND state IN ('delivered','observed')", (KEY_PREFIX + "%",))
        return row[0] if row and row[0] is not None else now - FIRST_LOOKBACK

    def pending(self, now=None):
        """Operations waiting for the owner, oldest first, each with the two commands that answer it."""
        now = self.core.clock() if now is None else now
        rows = []
        for row in self.core.store.query("SELECT * FROM operations WHERE state='awaiting_approval' ORDER BY created_at"):
            ident = short(row["id"])
            rows.append({"operation": row["id"], "kind": row["kind"], "target": row["target"],
                         "persona": row["persona"], "created_at": row["created_at"],
                         "age_days": int((now - row["created_at"]) // 86400),
                         "approve": f"/approve_{ident}_{review_code(row['params_hash'])}",
                         "reject": f"/reject_{ident}"})
        return rows

    def _approvals(self, now, zone):
        rows = self.pending(now)
        if not rows:
            return None
        lines = []
        for row in rows[:LIST_LIMIT]:
            age = f"{row['age_days']} дн" if row["age_days"] else "менее суток"
            lines.append(f"• {row['kind']} → {row['target'][:60]} · {age}\n  {row['approve']} · {row['reject']}")
        if len(rows) > LIST_LIMIT:
            lines.append(f"…и ещё {len(rows) - LIST_LIMIT} (весь список: butler-core pending)")
        return (f"Ждут вашего подтверждения ({len(rows)}). Без ответа операция не выполняется и через "
                f"{self.core.approval_expire_days()} дн. будет снята:\n" + "\n".join(lines))

    def _expired(self, now, zone):
        rows = self.core.store.query(
            "SELECT kind, COUNT(*) AS n FROM operations WHERE state='cancelled' AND reason LIKE 'expired:%'"
            " AND updated_at>? GROUP BY kind ORDER BY n DESC, kind", (self._since(now),))
        if not rows:
            return None
        total = sum(row["n"] for row in rows)
        return (f"Истекли без подтверждения и не выполнены ({total}): "
                + ", ".join(f"{row['kind']} ×{row['n']}" for row in rows))

    def _uncertain(self, now, zone):
        rows = self.core.store.query(
            "SELECT * FROM deliveries WHERE state='uncertain' AND kind NOT IN (%s) AND idempotency_key NOT LIKE ?"
            " AND updated_at>? ORDER BY updated_at" % ",".join("?" * len(NOT_LISTED)),
            (*NOT_LISTED, KEY_PREFIX + "%", self._since(now)))
        if not rows:
            return None
        lines = []
        for row in rows[:LIST_LIMIT]:
            # Another persona's text is not repeated to the owner's chat: only that something was lost, and where.
            preview = " ".join(row["content"].split())[:PREVIEW] if row["persona"] == "butler" else ""
            line = f"• {stamp(row['updated_at'], zone)} · {row['kind']} · {row['persona']}"
            lines.append(f"{line} — {preview}" if preview else line)
        if len(rows) > LIST_LIMIT:
            lines.append(f"…и ещё {len(rows) - LIST_LIMIT}")
        return ("Доставка не подтверждена — Telegram мог принять эти сообщения, а мог и нет; повторно я их не отправлял:\n"
                + "\n".join(lines))

    def build(self, now=None):
        now = self.core.clock() if now is None else now
        zone = self.core.delivery.rule()["zone"]
        sections = [text for text in (self._approvals(now, zone), self._expired(now, zone),
                                      self._uncertain(now, zone)) if text]
        return "Требует внимания:\n\n" + "\n\n".join(sections) if sections else None

    def maybe_send(self):
        now = self.core.clock()
        zone = self.core.delivery.rule()["zone"]
        if self.core.delivery.quiet(now):
            return {"sent": False, "reason": "quiet hours"}
        text = self.build(now)
        if text is None:
            return {"sent": False, "reason": "nothing to report"}
        day = dt.datetime.fromtimestamp(now, ZoneInfo(zone)).date().isoformat()
        result = self.core.notify("butler", text, "digest", KEY_PREFIX + day)
        if result is None:
            return {"sent": False, "reason": "owner notification target is not configured"}
        if result.get("duplicate"):
            return {"sent": False, "reason": "already sent today"}
        return {"sent": True, "delivery": result.get("id")}
