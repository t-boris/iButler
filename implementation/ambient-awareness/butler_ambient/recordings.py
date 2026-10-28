"""Owner-local recordings. This module is deliberately absent from the MCP tool list."""
from __future__ import annotations

import datetime as dt
import json
import re
import uuid
from pathlib import Path

from .event_frames import ZONE, local_day
from .observation_browser import _request, _days, _merged_gaps, _coverage, day_bounds, analysis_state

RETENTION = 30 * 86400
GAP_LABELS = {"owner-paused": "пауза", "analysis-buffer-full": "буфер заполнен",
              "service-stopped": "перезапуск", "analysis-expired": "не проанализировано",
              "analysis-missed": "не проанализировано", "analysis-busy": "не проанализировано",
              "analysis-failed": "не проанализировано"}


def _allowed(service):
    now = service.clock()
    return bool(service.gate.permits(now) and service.dialogue_gate.permits(now))


def _path(store, value):
    if not value:
        return None
    path = Path(value).resolve()
    roots = [(store.root / name).resolve() for name in ("raw", "preserved")]
    if not any(path.is_relative_to(root) for root in roots) or not path.is_file():
        return None
    return str(path)


def tag_capture(service, mid):
    """Called only by existing, permission-controlled capture paths."""
    meeting = getattr(service, "meeting", None)
    if meeting is not None and meeting.recording_id:
        service.store.tag_meeting_media(mid, meeting.recording_id,
                                       retain=bool(service.gate.permits(service.clock())))


def meeting_state(service):
    meeting = getattr(service, "meeting", None)
    key = meeting.recording_id if meeting is not None else None
    rows = service.store.execute("SELECT marked_at FROM recording_meetings WHERE id=?", (key,)) if key else []
    return {"detected": bool(key), "marked": bool(rows and rows[0]["marked_at"] is not None)}


def mark_meeting(service, args):
    if args != {"action": "mark"}:
        raise ValueError("invalid meeting action")
    with service.lock:
        if not _allowed(service):
            return {"ok": False, "message": "Требуется подтверждённый владелец и разрешение записи."}
        service.meeting.update()
        key = service.meeting.recording_id
        if not key:
            return {"ok": False, "message": "Текущая встреча не обнаружена."}
        # Close the current clip so retention starts at the explicit mark,
        # without silently retaining earlier camera bytes.
        service.archive.camera.finish()
        service.store.execute("UPDATE recording_meetings SET marked_at=COALESCE(marked_at,?) WHERE id=?",
                              (service.clock(), key))
        return {"ok": True, "message": "Аудио и видео с этого момента сохраняются на 30 дней при разрешённой записи.",
                "meeting": meeting_state(service)}


def _media(row, store, now):
    expiry = row.get("retained_until")
    path = None if expiry is not None and expiry <= now else (_path(store, row["preserved"]) or _path(store, row["path"]))
    return {"id": row["id"], "source": row["source"], "path": path,
            "preserved": bool(path and _path(store, row["preserved"])), "complete": bool(row["complete"]),
            "start": row["start"], "end": row["end"], "expires": expiry}


def _activity(item):
    if item.get("meeting_id"):
        return "call:" + item["meeting_id"]
    facts = item.get("facts", [])
    return next((str(f.get("activity") or f.get("detail") or f.get("topic"))
                 for f in facts if f.get("kind") == "activity"), "наблюдения")


def _sessions(items, gaps):
    """Consecutive minutes per source/activity; permission and capture gaps split runs."""
    sessions = []
    for source in sorted({i["source"] for i in items}):
        previous = None
        for item in sorted((i for i in items if i["source"] == source), key=lambda i: (i["start"], i["id"])):
            activity = _activity(item)
            split = previous is None or activity != previous["activity"] or item["start"] - previous["end"] > 90
            if not split:
                split = any(g["source"] in {source, "all", "analysis"} and g["start"] < item["start"]
                            and g["end"] > previous["end"] for g in gaps)
            if split:
                previous = {"type": "session", "id": "session:" + item["id"], "source": source,
                            "activity": activity, "start": item["start"], "end": item["end"], "observations": []}
                sessions.append(previous)
            previous["end"] = max(previous["end"], item["end"])
            previous["observations"].append(item)
    for session in sessions:
        rows = session["observations"]
        session["count"] = len(rows)
        session["content"] = sorted({label for row in rows for label, yes in (
            ("факты", bool(row.get("facts"))), ("текст", bool(row.get("transcripts"))),
            ("медиа", any(m["path"] for m in row.get("media", [])))) if yes})
    return sessions


def list_recordings(service, args):
    if not isinstance(args, dict):raise ValueError("invalid recordings request")
    session_id = args.get("session")
    if session_id is not None and not isinstance(session_id, str):raise ValueError("invalid session")
    source = args.get("source")
    standard = {k: v for k, v in args.items() if k != "session"}
    if source in {"generated", "display"}:standard.pop("source")
    request = _request(standard)
    if source in {"generated", "display"}:request["source"] = source
    with service.lock:
        if not _allowed(service):
            return {"hidden": True, "items": [], "days": [], "total": 0}
        store, now = service.store, service.clock()
        days = {d["day"]: d for d in _days(store, now)}
        transcript_rows = store.execute("SELECT * FROM recording_transcripts WHERE start>?", (now-RETENTION,))
        extra = []
        visual = getattr(service, "visual", None)
        if visual is not None:
            for artifact in store.execute("SELECT * FROM visual_artifacts ORDER BY created DESC"):
                path = visual.available(artifact)
                at = artifact["source_time"] or artifact["created"]
                extra.append({"type": "artifact", "id": "artifact:" + artifact["id"], "artifact_id": artifact["id"],
                              "source": "generated", "start": at, "end": at,
                              "title": artifact.get("caption") or "Изображение", "image": path, "media": []})
        display = getattr(service, "display", None)
        if display is not None:
            for card in display.snapshot().get("cards", []):
                extra.append({"type": "display", "id": "display:" + card["id"], "source": "display",
                              "start": card["at"], "end": card["at"], "title": card.get("title", "Дисплей"),
                              "card": card, "image": card.get("image"), "media": []})
        for row in [*transcript_rows, *extra]:
            day = local_day(row["start"])
            days.setdefault(day, {"day": day, "informative": 0, "empty": 0, "gaps": 0})
        days = sorted(days.values(), key=lambda d: d["day"], reverse=True)
        day = request["day"] or days[0]["day"]
        start, end = day_bounds(day)
        gaps = _merged_gaps(store, start, end, now)
        for gap in gaps:gap["label"] = GAP_LABELS.get(gap["reason"], gap["reason"])
        corrections = {r["subject"]: r["name"] for r in store.execute("SELECT * FROM corrections")}
        observations = store.execute("SELECT * FROM observations WHERE end>=? AND start<? ORDER BY start,id", (start, end))
        associated = store.execute('''SELECT DISTINCT r.observation_id,m.* FROM media m JOIN (
            SELECT observation_id,media_id FROM observation_media UNION SELECT id,media_id FROM observations
            UNION SELECT observation_id,media_id FROM event_frames) r ON r.media_id=m.id
            JOIN observations o ON o.id=r.observation_id WHERE o.end>=? AND o.start<?''', (start, end))
        by_observation = {}
        linked = set()
        for row in associated:
            by_observation.setdefault(row["observation_id"], []).append(row)
        day_transcripts = [t for t in transcript_rows if t["start"] < end and t["end"] >= start]
        # Sweep each source in chronological order rather than comparing every
        # observation with every retained transcript across the whole month.
        transcript_sources = {}
        for t in sorted(day_transcripts, key=lambda t: t["start"]):transcript_sources.setdefault(t["source"], []).append(t)
        cursors, active = {}, {}
        items = []
        for row in observations:
            facts = json.loads(row["facts"])
            media_rows = by_observation.get(row["id"], [])
            for fact in facts:
                if fact.get("subject") in corrections:
                    fact.update(identity=corrections[fact["subject"]], identity_basis="owner-confirmed")
            source = row["source"]
            pool = transcript_sources.get(source, [])
            cursor = cursors.get(source, 0)
            candidates = [t for t in active.get(source, []) if t["end"] > row["start"]]
            while cursor < len(pool) and pool[cursor]["start"] < row["end"]:
                if pool[cursor]["end"] > row["start"]:candidates.append(pool[cursor])
                cursor += 1
            cursors[source], active[source] = cursor, candidates
            transcripts = [t for t in candidates if t["start"] < row["end"]]
            linked.update(t["id"] for t in transcripts)
            media = [_media(m, store, now) for m in media_rows]
            items.append({"type": "observation", "id": row["id"], "source": row["source"], "start": row["start"],
                          "end": row["end"], "facts": facts, "transcripts": transcripts, "media": media,
                          "meeting_id": next((m["meeting_id"] for m in media_rows if m.get("meeting_id")), None),
                          "original": any(m["path"] for m in media)})
        for t in transcript_rows:
            if t["id"] not in linked and t["start"] < end and t["end"] >= start:
                items.append({"type": "observation", "id": "transcript:" + t["id"], "source": t["source"],
                              "start": t["start"], "end": t["end"], "facts": [], "transcripts": [t],
                              "media": [], "meeting_id": t["meeting_id"], "original": False})
        # Originals awaiting analysis are browseable too, rather than hidden
        # until facts happen to commit.
        pending = store.execute("""SELECT m.* FROM media m WHERE m.start<? AND m.end>=? AND (m.path IS NOT NULL OR m.preserved IS NOT NULL)
            AND NOT EXISTS(SELECT 1 FROM observation_media r WHERE r.media_id=m.id)
            AND NOT EXISTS(SELECT 1 FROM observations o WHERE o.media_id=m.id)
            AND NOT EXISTS(SELECT 1 FROM event_frames f WHERE f.media_id=m.id)""", (end, start))
        for row in pending:
            items.append({"type": "observation", "id": "media:" + row["id"], "source": row["source"],
                          "start": row["start"], "end": row["end"], "facts": [], "transcripts": [],
                          "media": [_media(row, store, now)], "meeting_id": row["meeting_id"], "original": True})
        if request["source"]:
            items = [i for i in items if i["source"] == request["source"]]
            gaps = [g for g in gaps if g["source"] in {request["source"], "all", "analysis"}]
        sessions = _sessions(items, gaps)
        if session_id is not None:
            session = next((s for s in sessions if s["id"] == session_id), None)
            rows = session["observations"] if session else []
        else:
            for session in sessions:session["observations"] = []  # children load in bounded pages on expansion
            rows = sessions + gaps + [i for i in extra if start <= i["start"] < end and (not request["source"] or i["source"] == request["source"])]
            rows.sort(key=lambda i: (i["start"], i.get("id", "")), reverse=True)
        offset, limit = request["offset"], request["limit"]
        return {"hidden": False, "day": day, "timezone": str(ZONE), "days": days, "total": len(rows),
                "items": rows[offset:offset+limit], "offset": offset, "limit": limit,
                "meeting": meeting_state(service), "summary": {"gaps": _coverage(gaps)},
                "analysis": analysis_state(store, now), "now": now}


def _selected(store, oid, mid):
    rows = store.execute('''SELECT m.* FROM media m WHERE m.id=? AND (
        ?='media:'||m.id OR EXISTS(SELECT 1 FROM observation_media WHERE observation_id=? AND media_id=m.id)
        OR EXISTS(SELECT 1 FROM observations WHERE id=? AND media_id=m.id)
        OR EXISTS(SELECT 1 FROM event_frames WHERE observation_id=? AND media_id=m.id))''', (mid, oid, oid, oid, oid))
    return rows[0] if rows else None


def control(service, args):
    if not isinstance(args, dict) or set(args) != {"action", "observation", "media"}:
        raise ValueError("invalid recording action")
    action, oid, mid = args["action"], args["observation"], args["media"]
    if action not in ("preserve", "delete") or not all(isinstance(x, str) and x for x in (oid, mid)):
        raise ValueError("invalid recording action")
    with service.lock:
        if not _allowed(service):return {"ok": False, "message": "Записи скрыты: требуется подтверждённый владелец."}
        store = service.store
        row = _selected(store, oid, mid)
        if not row:return {"ok": False, "message": "Запись больше недоступна."}
        if any(value and _path(store, value) is None and Path(value).exists() for value in (row["path"], row["preserved"])):
            return {"ok": False, "message": "Путь файла находится вне хранилища записей."}
        if action == "preserve":
            if not row["complete"] or not _media(row, store, service.clock())["path"]:
                return {"ok": False, "message": "Исходник недоступен или ещё записывается."}
            store.preserve(mid, service.clock())
        else:
            if not row["complete"]:return {"ok": False, "message": "Файл ещё записывается."}
            # Delete the selected file even when it backs several observations;
            # their facts/transcripts are independent records (DEC-005).
            store.delete(mid)
        return {"ok": True, "message": "Сохранено; фиксированный срок встречи не меняется." if action == "preserve" else "Файл удалён."}


def playback(service, args):
    if not isinstance(args, dict) or set(args) != {"observation", "media"} or not all(isinstance(v, str) for v in args.values()):
        raise ValueError("invalid playback request")
    with service.lock:
        if not _allowed(service):return {"hidden": True}
        row = _selected(service.store, args["observation"], args["media"])
        if not row or not row["complete"]:return {"unavailable": True}
        path = _media(row, service.store, service.clock())["path"]
        if not path:return {"unavailable": True}
        generation = service.gate.generation
        dialogue_generation = service.dialogue_gate.generation
    if Path(path).suffix == ".mjpeg":
        # The original remains byte-for-byte intact. AVKit needs a standard
        # playable container; the disposable derivative follows its lifetime.
        from . import processes as subprocess
        import shutil
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:return {"unavailable": True}
        folder = service.store.root / "playback";folder.mkdir(mode=0o700, exist_ok=True)
        target = folder / (row["id"] + ".mp4")
        if not target.exists():
            temporary = folder / (row["id"] + "." + uuid.uuid4().hex + ".mp4")
            try:
                frames = Path(path).read_bytes().count(b"\xff\xd8")
                rate = max(.1, frames / max(.1, row["end"]-row["start"]))
                done = subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-f", "mjpeg", "-framerate", str(rate),
                    "-i", path, "-an", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(temporary)],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60)
                if done.returncode:return {"unavailable": True}
                temporary.chmod(0o600);temporary.replace(target)
            finally:temporary.unlink(missing_ok=True)
        path = str(target)
    with service.lock:
        current = _selected(service.store, args["observation"], args["media"])
        if (not _allowed(service) or service.gate.generation != generation or service.dialogue_gate.generation != dialogue_generation
                or not current or not _media(current, service.store, service.clock())["path"]):
            return {"hidden": True}
        return {"path": path, "image": Path(path).suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}}


_NAV = re.compile(r"(?:покажи|открой).*(?:запис|что было на встрече)|(?:open|show).*(?:recordings|recording|meeting)", re.I)
_TIME = re.compile(r"(?<!\d)(\d{1,2})(?::([0-5]\d))?(?:\s*(утра|вечера|дня|ночи|am|pm))?(?!\d)", re.I)


def voice_target(text, now):
    if not _NAV.search(text):return None
    current = dt.datetime.fromtimestamp(now, ZONE)
    date_match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", text)
    date = dt.date.fromisoformat(date_match[1]) if date_match else current.date()
    if re.search(r"вчера|yesterday", text, re.I):date -= dt.timedelta(days=1)
    remaining = text.replace(date_match[0], "") if date_match else text
    match = _TIME.search(remaining)
    target = {"day": date.isoformat()}
    if match:
        hour, minute, period = int(match[1]), int(match[2] or 0), (match[3] or "").lower()
        if period in {"вечера", "дня", "pm"} and hour < 12:hour += 12
        elif period in {"утра", "ночи", "am"} and hour == 12:hour = 0
        if hour > 23:return target
        target["at"] = dt.datetime.combine(date, dt.time(hour, minute), tzinfo=ZONE).timestamp()
    return target


def navigate_voice(service, text, *, authorized):
    target = voice_target(text, service.clock())
    if target is None:return False
    # Recognised navigation requests are consumed even if access is refused;
    # they must never fall through to the cloud conversation.
    if authorized and _allowed(service):
        serial = getattr(service, "recordings_navigation", {}).get("serial", 0) + 1
        service.recordings_navigation = {**target, "serial": serial}
        ensure = getattr(service, "ensure_panel", None)
        if callable(ensure):ensure()
    return True
