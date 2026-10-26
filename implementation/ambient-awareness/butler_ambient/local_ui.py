"""Owner-local presentation. This richer view is deliberately absent from MCP."""
from __future__ import annotations

import json
import base64


def snapshot(service, *, want_preview=False):
    from .voice_catalog import profiles
    with service.lock:
        result = service.status()
        result['storage'] = service.store.storage_status()
        from .event_frames import local_day
        result['storage']['event_frames'] = service.store.event_frame_status(local_day(service.clock()))
        result.pop("agreements", None)
        result["last_capture"] = {r["source"]: r["at"] for r in service.store.execute(
            "SELECT source,MAX(end) AS at FROM media GROUP BY source")}
        result["last_analysis"] = {r["source"]: r["at"] for r in service.store.execute(
            "SELECT source,MAX(end) AS at FROM observations GROUP BY source")}
        result["recent"] = []
        for row in service.store.execute("SELECT source,start,end,facts FROM observations ORDER BY end DESC LIMIT 5"):
            result["recent"].append({"source": row["source"], "at": row["end"], "details": [
                fact.get("detail", "") for fact in json.loads(row["facts"]) if fact.get("detail")]})
        result["voice"] = {"selected": service.store.get("voice_profile"),
                           "enabled": service.store.get("voice_enabled", False),
                           "profiles": profiles(service.store),
                           "state": getattr(getattr(service, "voice", None), "state", "not-configured"),
                           "face_wake_dialogue": service.store.get("face_wake_dialogue", False),
                           "dialogue_allowed": service.voice_gate.permits(service.clock()),
                           "last_heard": getattr(service.voice, "last_heard", ""),
                           "last_reply": getattr(service.voice, "last_reply", ""),
                           "phase": getattr(service.voice, "phase", "idle"),
                           "last_cancel_reason": getattr(service.voice, "last_cancel_reason", None),
                           "echo_cancellation": getattr(service.inputs, "audio_status", {}).get("echo_cancellation") is True}
        if service.voice is not None:
            general = service.voice.general_permitted()
            result["voice"]["dialogue_mode"] = ("private" if result["voice"]["dialogue_allowed"]
                                                else "general" if general else "off")
            if not result["voice"]["dialogue_allowed"]:
                # Never show owner-private dialogue without the lease; a
                # general conversation stays visible and is not cancelled.
                if service.voice.private_state() and not service.voice.turn_committed:
                    # Hiding is enough while a turn is being answered (turn-taking REQ-002); cancelling it cut replies.
                    service.voice.cancel(reason="private-view-hidden", private_only=True)
                if not general or service.voice.visible_mode != "general":
                    result["voice"]["last_heard"] = result["voice"]["last_reply"] = ""
                else:
                    result["voice"]["last_heard"] = service.voice.last_heard
                    result["voice"]["last_reply"] = service.voice.last_reply
            result["voice"]["diagnostics"] = service.voice.diagnostics()
            result["voice"]["debug"] = (service.voice.debug_snapshot() if result["voice"]["dialogue_allowed"]
                                        else service.voice.debug_snapshot(general_only=True) if general
                                        else {"events": [], "microphone": None})
        if getattr(service,"listening",None) is not None:
            listening=service.listening.snapshot()
            result["voice"]["listening"]=listening
            if listening["enabled"]:
                result["voice"]["last_heard"]=listening["last_heard"]
                debug=result["voice"].setdefault("debug",{"events":[],"microphone":None})
                debug["events"]=sorted(debug["events"]+listening["events"],key=lambda e:e["at"])[-60:]
        result["voice"]["meeting"] = service.meeting.snapshot()
        result["voice"]["attention"] = service.attention.snapshot() if getattr(service, "attention", None) is not None else None
        result["voice"]["smart_turn"] = service.smart_turn.snapshot() if getattr(service, "smart_turn", None) is not None else None
        result["voice"]["brain"] = service.brain.snapshot() if getattr(service, "brain", None) is not None else None
        result["voice"]["calls"] = service.calls.snapshot() if getattr(service, "calls", None) is not None else None
        from . import __version__
        result["version"] = __version__
        result["work"] = service.work.snapshot()
        result["core_link"] = service.core_link.snapshot() if getattr(service, "core_link", None) is not None else None
        result["knowledge"] = service.knowledge.snapshot()
        result["voice"]["disclosure"] = service.disclosures.snapshot()
        lease = service.voice_gate
        age = service.clock() - lease.last_check
        capture_age = service.clock() - lease.last_capture
        result["voice"]["participant"] = {"fresh": 0 <= age <= 2.5 and 0 <= capture_age <= 2.5,
            "check_age_seconds": round(age, 2) if 0 <= age < 3600 else None,
            "capture_age_seconds": round(capture_age, 2) if 0 <= capture_age < 3600 else None,
            "consecutive_checks": min(lease.streak, 3), "required_checks": 3,
            "owner_camera_confirmed": bool(service.dialogue_gate.permits(service.clock()) and service.dialogue_gate.face_streak >= 3),
            "owner_voice_confirmed": bool(service.dialogue_gate.permits(service.clock()) and service.dialogue_gate.voice_streak >= 3),
            "confirmed_by": service.dialogue_gate.confirmed_by,
            "session_closed_reason": service.dialogue_gate.closed_reason, "session_closes": list(service.dialogue_gate.closes)}
        voice_failure = getattr(service.verifier, "last_voice_failure", None)
        result["voice"]["participant"]["voice_check"] = voice_failure if type(voice_failure) is str else None
        if isinstance(getattr(service.inputs, "audio_status", None), dict):
            result["voice"]["audio"] = dict(service.inputs.audio_status)
            result["voice"]["playback"] = dict(service.inputs.playback)
        signal = getattr(service.inputs, "input_signal", None)
        fresh = isinstance(signal, dict) and 0 <= service.clock() - signal["at"] <= 2.5
        input_status=result["voice"].get("audio",{})
        result["voice"]["input"] = {"working": fresh and input_status.get("input_muted") is not True and input_status.get("input_volume") != 0 and input_status.get("input_verified") is not False,
            "muted": input_status.get("input_muted"),
            "gain": input_status.get("input_volume"),
            "rms_dbfs": signal["rms_dbfs"] if fresh else None,
            "name": input_status.get("actual_microphone") or service.config.get("expected_microphone", "Выбранный микрофон"),
            "selected_name": service.config.get("expected_microphone", "Выбранный микрофон"),
            "selection_verified": input_status.get("input_verified"),
            "device": str(service.config.get("microphone", "0"))}
        at = getattr(service.inputs, "last_camera_at", None)
        restarts = getattr(service.inputs, "camera_restarts", 0)
        failure = getattr(service.inputs, "camera_failure", None)
        camera_status = getattr(service.inputs, "camera_status", {})
        result["voice"]["camera_input"] = {"working": isinstance(at, (int, float)) and 0 <= service.clock() - at <= 2.5,
            "restarts": restarts if type(restarts) is int else 0,
            "backend": "native-avfoundation" if getattr(service.inputs, "camera_name", None) and getattr(service.inputs, "camera_helper", None) else "ffmpeg",
            "native_status": camera_status if type(camera_status) is dict else {},
            "failure": failure if failure in {"video-stalled", "video-exited", "video-arguments", "video-size", "video-permission", "video-selected-device", "video-format", "video-input", "video-open", "video-output", "video-start"} else None}
        result["voice"]["ready"] = bool(service.voice is not None and service.voice.ready)
        result["voice"]["commands"] = service.commands.snapshot()
        result["voice"]["owner_setup"] = service.owner_setup.snapshot()
        result["preview"] = None
        result["visual"] = service.visual.snapshot() if service.visual is not None else None
        if not result["recording_allowed"] and result["visual"] is not None:
            # Gallery metadata may remain; image paths are hidden on a pause.
            for artifact in result["visual"].get("artifacts", []):
                artifact.pop("path", None)
        display = getattr(service, "display", None)
        result["display"] = display.snapshot(dialogue_allowed=bool(result["voice"].get("dialogue_allowed")),
                                             recording_allowed=bool(result["recording_allowed"])) if display is not None else None
        result["feed"] = feed(service, result)
        destination = service.store.root / "raw/ui-preview.jpg"
        if not result["recording_allowed"]:
            destination.unlink(missing_ok=True)
        elif want_preview:
            live=service.preview_frame
            if live is not None and 0<=service.clock()-live[1]<=2.5 and service.archive.enabled:
                # This private Unix-socket response is not an MCP result. No
                # original file is created just to display a fresh camera feed.
                result['preview']={'jpeg_base64':base64.b64encode(live[0]).decode('ascii'),'captured_at':live[1]}
                destination.unlink(missing_ok=True)
                return result
            if live is not None:service.preview_frame=None
            rows = service.store.execute("SELECT path,end FROM media WHERE source='camera' AND path IS NOT NULL AND end>=? ORDER BY end DESC LIMIT 1", (service.clock() - 2.5,))
            if rows:
                from pathlib import Path
                data = Path(rows[0]["path"]).read_bytes()
                start = data.rfind(b"\xff\xd8")
                end = data.find(b"\xff\xd9", start)
                if start >= 0 and end > start:
                    temporary = destination.with_suffix(".tmp")
                    temporary.write_bytes(data[start:end + 2])
                    temporary.chmod(0o600)
                    temporary.replace(destination)
                    result["preview"] = {"path": str(destination), "captured_at": rows[0]["end"]}
        return result


EVENT_KINDS = {"heard": "heard", "reply": "reply", "error": "error", "interrupted": "error", "display": "display",
               "hearing-unavailable": "error", "reply-blocked": "event", "reply-blocked-unchecked": "event"}
EVENT_LABELS = {"display": "ПОКАЗ", "attention": "ВНИМАНИЕ", "owner-busy": "ЗАНЯТ С ДРУГИМ", "brain": "АГЕНТ", "session": "СЕССИЯ", "call-summary": "ИТОГИ ЗВОНКА", "heard": "РАСПОЗНАНО", "wake": "ОБРАЩЕНИЕ", "address-accepted": "ПРИНЯТО", "address-rejected": "ОТКЛОНЕНО",
                "reply": "ОТВЕТ", "error": "ОШИБКА", "interrupted": "ПРЕРВАНО", "transcription": "РАСПОЗНАВАНИЕ",
                "synthesis": "ГОЛОС", "playback": "ДИНАМИК", "completed": "ЗАВЕРШЕНО", "reply-blocked": "ОТВЕТ ЗАБЛОКИРОВАН",
                "reply-blocked-unchecked": "ПРОВЕРКА РЕПЛИКИ НЕДОСТУПНА", "participant-check-wait": "ОЖИДАНИЕ ПРОВЕРКИ",
                "routed": "ДИАЛОГ", "routed-general": "ОБЩИЙ РАЗГОВОР", "general-not-addressed": "НЕТ ОБРАЩЕНИЯ",
                "hearing-replied": "ПРОВЕРКА МИКРОФОНА", "hearing-unavailable": "ОШИБКА ГОЛОСОВОЙ ПРОВЕРКИ",
                "outcome": "ИСХОД", "backchannel": "ПОДДАКИВАНИЕ", "silence-set": "ТИШИНА", "silence-end": "ТИШИНА СНЯТА",
                "private-withheld": "ЛИЧНОЕ НЕ ОЗВУЧЕНО", "continuation-wait": "ЖДУ ПРОДОЛЖЕНИЯ",
                "heard-during-playback": "УСЛЫШАНО ВО ВРЕМЯ РЕЧИ", "echo": "ЭХО"}
ARTIFACT_LABELS = {"captured": "СНИМОК", "imported": "НАЙДЕННЫЙ МАТЕРИАЛ", "generated": "СОЗДАНО МОДЕЛЬЮ"}
FEED_LIMIT = 80


def feed(service, result):
    """One timeline for the owner panel: dialogue, service events, images and the notices Butler sent.

    Entries carry who, when and what kind they are, so the panel can draw each
    differently. Only what the owner may currently see is included: dialogue
    follows the private lease, image paths follow the recording state, and the
    Telegram notices are shown only while the verified owner is present."""
    entries = []
    for event in result.get("voice", {}).get("debug", {}).get("events", []):
        key = event.get("event", "")
        entries.append({"id": f"voice:{event.get('at')}:{key}", "at": event.get("at"), "who": event.get("who", "Система"),
                        "kind": EVENT_KINDS.get(key, "event"), "label": EVENT_LABELS.get(key, key),
                        "text": event.get("detail", "")})
    visual = result.get("visual")
    for artifact in (visual.get("artifacts", []) if isinstance(visual, dict) else []):
        if not isinstance(artifact, dict) or not artifact.get("path"):
            continue
        entries.append({"id": f"artifact:{artifact.get('id')}", "at": artifact.get("at"), "who": "Butler", "kind": "image",
                        "label": ARTIFACT_LABELS.get(artifact.get("kind"), "ИЗОБРАЖЕНИЕ"),
                        "text": artifact.get("caption") or "", "image": artifact["path"]})
    notices = getattr(service, "notices", None)
    if notices is not None and result.get("voice", {}).get("dialogue_allowed"):
        notices.start()
        entries.extend(notices.entries())
    entries = [e for e in entries if isinstance(e.get("at"), (int, float))]
    entries.sort(key=lambda e: e["at"])
    return merge_heard(entries)[-FEED_LIMIT:]


def merge_heard(entries):
    """Passive hearing and the dialogue both report the same utterance; the panel shows it once.

    The earlier line (an unnamed voice) keeps its place and time, and the later attributed
    line lends it its author, so the row neither duplicates nor jumps."""
    result = []
    for entry in entries:
        if entry.get("kind") == "heard":
            twin = next((e for e in reversed(result) if e.get("kind") == "heard" and e.get("text") == entry.get("text")
                         and 0 <= entry["at"] - e["at"] <= 20), None)
            if twin is not None:
                if twin.get("who") == "Голос" and entry.get("who") != "Голос":
                    twin["who"] = entry["who"]
                continue
        result.append(entry)
    return result


def observe(service, source):
    result = service.request_fresh(source)
    if result.get("status") != "captured":
        return result
    with service.lock:
        if not service.gate.permits(service.clock()):
            return {"status": "paused", "source": source}
        mid = result["source_id"]
        service.store.expire(service.clock())
        rows = service.store.execute("SELECT path,start FROM media WHERE id=?", (mid,))
        if not rows or not rows[0]["path"]:
            return {"status": "unavailable", "source": source}
        return {"status": "ok", "source": source, "source_id": mid,
                "path": rows[0]["path"], "captured_at": rows[0]["start"]}
