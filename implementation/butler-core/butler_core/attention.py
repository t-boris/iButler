"""What needs the owner: one short message a day (BUG-021, BUG-022).

Things the core cannot settle by itself used to sit in its database unseen. This module gathers them into a single
``digest`` delivery, sent at most once per local day and never during quiet hours:

* deliveries whose outcome nobody could confirm (they are never resent, see ``delivery``);
* more sections are added here as the owner decides what else must not stay silent.

Nothing is sent when nothing needs attention, and an entry is listed until a digest that carried it was delivered.
"""
from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

KEY_PREFIX = "attention:"
FIRST_LOOKBACK = 3 * 86400
LIST_LIMIT = 10
PREVIEW = 80
MONTHS = ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "нояб", "дек")
# "imported" rows are history copied from the gateway's failed queue, not something that happened since the last digest.
# Approval requests have their own single resend and, later, their own section.
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
        sections = [text for text in (self._uncertain(now, zone),) if text]
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
