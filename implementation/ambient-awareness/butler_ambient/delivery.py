"""Durable at-most-once delivery; ambiguous outcomes are never blindly retried."""
from __future__ import annotations

import datetime as dt
import hashlib
import importlib.util
import json
from . import processes as subprocess
import time
from pathlib import Path
from zoneinfo import ZoneInfo


class TransportBusy(RuntimeError):
    """A transport refused before producing any output: nothing was delivered."""


class NothingSent(RuntimeError):
    """The transport could not hand the message over at all: the reservation is released."""


class DisplayTransport:
    """Publish autonomous results as local Display cards without spoken output."""

    def __init__(self, service):
        self.service = service

    def send(self, text, token=None, *, key=None, kind="status", allow_telegram=True, require_local_history=False):
        titles = {"daily": "Дневная сводка", "advice": "Инициативный совет"}
        if kind not in titles:
            raise NothingSent("unsupported Display notice")
        with self.service.lock:
            if token is not None and token.is_set():
                raise NothingSent("delivery canceled by observation policy")
            if (kind == "daily" or require_local_history) and not self.service.gate.permits(self.service.clock()):
                from .recordings import history_visible
                if not history_visible(self.service):
                    raise NothingSent("local history unavailable")
            card = self.service.display.present(
                {"kind": "text", "title": titles[kind], "markdown": text}, origin="ambient")
            configured_telegram = self.service.store.get("schedule_telegram_" + kind, False) is True
            send_telegram = configured_telegram and allow_telegram
        if configured_telegram and not allow_telegram:
            self.service.display.delivery_status(card["id"], "Telegram: ручной результат оставлен в Display.")
        if send_telegram:
            try:
                with self.service.lock:
                    if not self.service.gate.permits(self.service.clock()):
                        raise NothingSent("owner verification required for Telegram")
                from .conversation import Tokens
                external_token = Tokens(token, self.service.analysis_token) if token is not None else self.service.analysis_token
                receipt = self.service.delivery.transport.send(text, token=external_token, key=str(key) + ":telegram", kind=kind)
                status = "Передано в очередь Telegram." if str(receipt).startswith("core:") else "Отправлено в Telegram."
            except NothingSent:
                status = "Не отправлено в Telegram: требуется подтверждение владельца или доступ к каналу. Повтор не выполнялся."
            except Exception:
                status = "Результат отправки в Telegram неизвестен; повтор не выполнялся."
            self.service.display.delivery_status(card["id"], status)
        return "display:" + card["id"]


CORE_CLIENT = 'lib/butler_core_client.py'


def core_client(workspace):
    """The butler-core client deployed into the Butler workspace, or None before the core is installed."""
    path = Path(workspace) / CORE_CLIENT
    if not path.is_file() or path.is_symlink():
        return None
    spec = importlib.util.spec_from_file_location('butler_core_client', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CoreNotices:
    """The owner notices butler-core delivered or queued, for the owner panel's feed (metadata plus text).

    Polled on a daemon thread so a slow or absent core never delays a UI
    snapshot taken under the service lock. Nothing here is sent anywhere."""

    INTERVAL = 5.0

    def __init__(self, core, limit=40, interval=INTERVAL):
        self.core, self.limit, self.interval = core, limit, interval
        self.rows, self.thread, self.stopped = [], None, False
        self.lock = __import__("threading").Lock()

    def start(self):
        import threading
        if self.core is None or self.thread is not None:
            return
        self.thread = threading.Thread(target=self._loop, name="core-notices", daemon=True)
        self.thread.start()

    def _loop(self):
        while not self.stopped:
            self.poll()
            time.sleep(self.interval)

    def poll(self):
        if self.core is None:
            return
        try:
            rows = self.core.Client(timeout=2.0).call("delivery.list", limit=self.limit)
        except Exception:
            return      # the last good list stays on display; a dead core is shown elsewhere
        with self.lock:
            self.rows = [row for row in rows if isinstance(row, dict)]

    def entries(self):
        labels = {"pending": "в очереди", "deferred": "отложено до утра", "sending": "отправляется", "delivered": "доставлено",
                  "failed": "не доставлено", "cancelled": "отменено", "uncertain": "исход неизвестен", "observed": "ответ в чате"}
        with self.lock:
            rows = list(self.rows)
        return [{"id": "delivery:" + str(row.get("id")), "at": row.get("created_at"), "who": "Butler → Telegram",
                 "kind": "notification", "label": (row.get("kind") or "notice") + " · " + labels.get(row.get("state"), str(row.get("state"))),
                 "text": row.get("content") or row.get("reason") or ""}
                for row in rows if row.get("kind") != "imported" and row.get("persona") == "butler"]


class SwitchedTransport:
    """Advice and daily summaries through butler-core delivery while its switch is on (W3, REQ-024).

    The core then owns quiet hours, initiative limits and transmission; the
    ambient ledger still reserves first, so a message is handed over at most
    once. With the switch off every message takes the legacy transport."""

    def __init__(self, core, fallback, persona="butler"):
        self.core, self.fallback, self.persona = core, fallback, persona

    def send(self, text, token=None, *, key=None, kind="status"):
        if not (self.core and self.core.switched("delivery")):
            return self.fallback.send(text, token=token, key=key, kind=kind)
        if token is not None and token.is_set():
            raise RuntimeError("delivery canceled by observation policy")
        try:
            result = self.core.Client().notify(text, f"ambient:{key}", kind=kind, persona=self.persona)
        except self.core.CoreUnavailable as error:
            raise NothingSent(f"butler-core unavailable: {error}") from None
        except self.core.CoreError as error:
            raise NothingSent(f"butler-core refused: {error.code}") from None
        if not result or not result.get("id"):
            raise NothingSent("butler-core recorded no delivery")
        return "core:" + result["id"]


ZONE = ZoneInfo("America/Chicago")
TOPICS = {"work", "rest", "reading", "entertainment"}


def quiet(now):
    return dt.datetime.fromtimestamp(now, ZONE).hour >= 22 or dt.datetime.fromtimestamp(now, ZONE).hour < 8


class Telegram:
    def __init__(self, target, account="butler", executable="openclaw"):
        self.executable = executable
        self.target, self.account = target, account

    def send(self, text, token=None, **meta):
        return self._send(text, token=token)

    def send_image(self, path, caption, token=None):
        from pathlib import Path
        from .owner_request import OWNER
        image = Path(path)
        if str(self.target).removeprefix("telegram:") != OWNER or self.account != "butler":
            raise ValueError("only owner private image delivery allowed")
        if not image.is_absolute() or not image.is_file() or image.is_symlink() or image.suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".tiff", ".tif", ".bmp"}:
            raise ValueError("local image required")
        return self._send(caption, token=token, media=str(image))

    def _send(self, text, token=None, media=None):
        if not self.target:
            raise RuntimeError("existing Butler Telegram conversation not configured")
        if token is not None and token.is_set():
            raise RuntimeError("delivery canceled by observation policy")
        started = time.monotonic()
        command = [self.executable, "message", "send", "--channel", "telegram", "--account", self.account,
                   "--target", self.target, "--message", text, "--json"]
        if media is not None: command.extend(["--media", media])
        process = subprocess.Popen(command,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            while True:
                try:
                    output, _ = process.communicate(timeout=.2)
                    break
                except subprocess.TimeoutExpired:
                    if (token is not None and token.is_set()) or time.monotonic() - started >= 45:
                        raise RuntimeError("Telegram outcome uncertain")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
        if process.returncode:
            raise RuntimeError("Telegram delivery outcome uncertain")
        try:
            data = json.loads(output)
            payload = data.get("payload", data.get("result", data))
            receipt = payload.get("messageId", payload.get("message_id"))
            if not receipt:
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise RuntimeError("Telegram receipt unavailable; outcome uncertain") from None
        return str(receipt)


class Delivery:
    def __init__(self, store, transport):
        self.store, self.transport = store, transport

    def send(self, key, kind, text, now, *, topic=None, created_at=None, token=None, transport=None):
        if kind not in {"advice", "daily"}:
            raise ValueError("invalid autonomous message kind")
        day = dt.datetime.fromtimestamp(now, ZONE).date().isoformat()
        # Keep the complete bounded delivery attempt outside quiet hours.
        if quiet(now) or quiet(now + 45):
            return "quiet-hours"
        if token is not None and token.is_set():
            return "paused"
        if kind == "advice":
            if topic not in TOPICS or self.store.get("muted", False) or topic in self.store.get("disabled_topics", []):
                return "muted"
            if created_at is None or not 0 <= now - created_at <= 120:
                return "stale"
        with self.store.lock:
            db = self.store.db
            try:
                db.execute("BEGIN IMMEDIATE")
                if db.execute("SELECT 1 FROM deliveries WHERE id=?", (key,)).fetchone():
                    db.rollback()
                    return "duplicate"
                if kind == "advice":
                    count = db.execute("SELECT COUNT(*) FROM deliveries WHERE kind='advice' AND day=?", (day,)).fetchone()[0]
                    last = db.execute("SELECT MAX(at) FROM deliveries WHERE kind='advice'").fetchone()[0]
                    if count >= 3 or (last is not None and now - last < 3600):
                        db.rollback()
                        return "quota"
                # Reserve BEFORE transmission, including across processes/restarts.
                db.execute("INSERT INTO deliveries VALUES (?,?,?,?,?)", (key, kind, day, now, "reserved"))
                db.commit()
            except Exception:
                db.rollback()
                raise
        try:
            attempt_started = time.monotonic()
            try:
                receipt = (transport or self.transport).send(text, token=token, key=key, kind=kind)
            except TransportBusy:
                # Nothing was spoken, so the reserved message goes through the
                # default transport instead of becoming an uncertain loss.
                if transport is None or transport is self.transport: raise
                attempt_started = time.monotonic()
                receipt = self.transport.send(text, token=token, key=key, kind=kind)
        except NothingSent:
            # Nothing left this process, so the slot is given back instead of being counted as lost.
            self.store.execute("DELETE FROM deliveries WHERE id=?", (key,))
            return "unavailable"
        except Exception:
            self.store.execute("UPDATE deliveries SET state='uncertain' WHERE id=?", (key,))
            return "uncertain"
        # Receipt time is a conservative bound on actual delivery. The next
        # interval starts here, rather than at the beginning of a slow request.
        delivered_at = now + max(0, time.monotonic() - attempt_started)
        delivered_day = dt.datetime.fromtimestamp(delivered_at, ZONE).date().isoformat()
        self.store.execute("UPDATE deliveries SET state=?,at=?,day=? WHERE id=?", ("sent:" + receipt, delivered_at, delivered_day, key))
        return "sent"


def advice_candidate(rows, now):
    """Autonomous opportunities in all four agreed domains; no action execution."""
    recent = [r for r in rows if now - r["end"] <= 120]
    if not recent:
        return None
    activities = {f.get("activity") for r in recent for f in r["facts"] if f["kind"] == "activity" and f["confidence"] >= 0.65}
    kinds = {f["kind"] for r in recent for f in r["facts"] if f["confidence"] >= 0.65}
    # Duration is the union of actual work evidence, never a count of overlapping sources.
    spans = sorted((r["start"], r["end"]) for r in rows if any(
        f.get("activity") == "work" and f["confidence"] >= .65 for f in r["facts"]))
    tail_start = tail_end = None
    for a, b in spans:
        if tail_end is None or a - tail_end > 5:
            tail_start, tail_end = a, b
        else:
            tail_end = max(tail_end, b)
    if "work" in activities and tail_start is not None and tail_end - tail_start >= 3600:
        topic, suggestion, benefit = "rest", "Можно сделать небольшой перерыв.", "Смена занятия может помочь восстановить внимание."
    elif "reading" in activities or "book" in kinds:
        topic, suggestion, benefit = "reading", "Можно записать одну мысль из прочитанного.", "Короткая заметка поможет вернуться к ней позже."
    elif "work" in activities:
        topic, suggestion, benefit = "work", "Перед переключением можно записать следующий шаг.", "Так будет проще вернуться к задаче."
    elif activities & {"rest", "entertainment", "youtube"}:
        topic, suggestion, benefit = "entertainment", "Если материал понравился, можно сохранить его на потом.", "Так его будет проще найти снова."
    else:
        return None
    evidence = sorted({r["id"] for r in recent})
    key = hashlib.sha256(json.dumps([topic, evidence]).encode()).hexdigest()
    labels = {"work": "работа", "reading": "чтение", "rest": "отдых", "entertainment": "развлечение",
              "youtube": "видео", "book": "книга", "activity": "занятие"}
    basis = ", ".join(labels.get(x, x) for x in sorted(x for x in activities if x)) or ", ".join(labels.get(x, x) for x in sorted(kinds))
    return {"key": key, "topic": topic, "created_at": now,
            "text": f"{suggestion}\nВозможная польза: {benefit}\nОснование: возможно, {basis}; вывод модели может быть неточным.\nНаблюдения: {', '.join(evidence[:6])}"}
