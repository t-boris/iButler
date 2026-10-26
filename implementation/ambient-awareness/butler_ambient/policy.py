"""Participant authorization is independent of model-inferred identity."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .dialogue_state import Scene


@dataclass(frozen=True)
class Evidence:
    captured_at: float
    camera_ok: bool
    microphone_ok: bool
    verified: frozenset[str] = frozenset()
    certain: bool = False
    voice_ids: frozenset[str] = frozenset()
    camera_ids: frozenset[str] = frozenset()
    camera_certain: bool = False
    speech_detected: bool = False
    voice_certain: bool = False
    scene: Scene = field(default_factory=Scene)      # head orientation, lip motion, company: metadata only


CLOSE_TEXT = {"company": "рядом кто-то ещё", "absence": "вас не было 15 минут", "disabled": "диалог выключен",
              "owner-paused": "пауза", "consent-revoked": "согласие отозвано", "settings-change": "изменены настройки",
              "service-stopped": "сервис остановлен", "owner-voice-enrollment": "запись голоса", "owner-reference-updated": "обновлён образец",
              "closed": "закрыта"}


class DialogueGate:
    """DEC-066 «один дома» session. It never grants recording or writes context.

    Opening keeps DEC-006/DEC-015: three verified owner checks (face or voice, identity thresholds untouched),
    collected while fresh checks arrive. Once open, the session is held until positive evidence of another
    person, ABSENCE seconds without the owner seen or heard, or an explicit close (pause, consent, settings).
    Stale checks, silence, a turned head, darkness and capture gaps do not close it: being alone is the
    owner's normal state and verification is the exception for company, not a ritual every 2.5 seconds."""
    def __init__(self):
        self.allowed = False
        self.streak = 0
        self.last_check = self.last_capture = float("-inf")
        self.generation = 0
        self.face_streak = self.voice_streak = 0
        self.face_seen = self.voice_seen = float("-inf")
        self.confirmed_by = None
        self.owner_seen = float("-inf")
        self.closed_reason = None
        self.trusted = frozenset({"owner"})
        self.closes = deque(maxlen=20)      # (at, reason) of every close: the owner asks why Butler fell silent
        self.last_now = None

    GRACE = 10.0     # seconds a building streak survives without the owner's face/voice (silence, a turned head)
    ABSENCE = 900.0  # seconds without the owner seen or heard that close an open session

    def invalidate(self, reason="closed"):
        if self.allowed:
            self.generation += 1
            self.closes.append({"at": self.last_now, "reason": reason})
        self.allowed = False
        self.streak = 0
        self.face_streak = self.voice_streak = 0
        self.face_seen = self.voice_seen = float("-inf")
        self.confirmed_by = None
        self.owner_seen = float("-inf")
        self.closed_reason = reason

    @staticmethod
    def company(evidence, trusted):
        """Positive evidence of someone who is not the owner or a household member: a certain non-trusted face or
        voice, or more faces than the check could identify as trusted. An unidentified single face is most often
        the owner in bad light and is not company."""
        other_face = evidence.camera_ok and evidence.camera_certain and bool(evidence.camera_ids - trusted)
        other_voice = evidence.voice_certain and bool(evidence.voice_ids - trusted)
        scene = getattr(evidence, "scene", None)
        faces = getattr(scene, "faces", 0) if scene is not None and getattr(scene, "available", False) else 0
        crowd = faces >= 2 and not (evidence.camera_certain and evidence.camera_ids <= trusted and len(evidence.camera_ids) >= faces)
        return other_face or other_voice or crowd

    def check(self, evidence, now, enabled, trusted=None):
        trusted = self.trusted if trusted is None else trusted
        self.last_now = now
        fresh = (enabled and evidence.microphone_ok and 0 <= now - evidence.captured_at <= Gate.MAX_AGE
                 and evidence.captured_at > self.last_capture)
        self.last_check, self.last_capture = now, max(self.last_capture, evidence.captured_at)
        owner_face = (evidence.camera_ok and evidence.camera_certain and "owner" in evidence.camera_ids
                      and evidence.camera_ids <= trusted)
        owner_voice = evidence.voice_certain and "owner" in evidence.voice_ids and evidence.voice_ids <= trusted
        if self.allowed:
            # Holding: only company, absence or disabling close the session.
            if not enabled:
                self.invalidate("disabled")
            elif fresh and self.company(evidence, trusted):
                self.invalidate("company")
            else:
                if fresh and (owner_face or owner_voice): self.owner_seen = now
                if now - self.owner_seen > self.ABSENCE: self.invalidate("absence")
            return self.allowed
        if now - self.last_check > Gate.MAX_AGE: self.face_streak = self.voice_streak = 0
        face = fresh and owner_face
        other_face = fresh and evidence.camera_ok and evidence.camera_certain and bool(evidence.camera_ids - trusted)
        # Three verified owner seconds are still required (DEC-006); they need not be consecutive. A second
        # with no face (a turned head) or no speech (a pause) keeps the count for GRACE seconds; another
        # person, or a longer absence, resets it. Identity thresholds are untouched.
        unknown_face = fresh and evidence.camera_ok and not evidence.camera_certain       # someone the check cannot identify
        nobody_visible = fresh and evidence.camera_ok and evidence.camera_certain and not evidence.camera_ids
        if face:
            self.face_streak, self.face_seen = min(self.face_streak + 1, Gate.REQUIRED_CHECKS), now
        elif other_face or unknown_face or (fresh and not nobody_visible) or now - self.face_seen > self.GRACE:
            self.face_streak = 0            # only certain "nobody in frame" or a capture gap keeps the count
        voice = fresh and owner_voice
        other_voice = fresh and evidence.voice_certain and bool(evidence.voice_ids - trusted)
        unknown_voice = fresh and evidence.speech_detected and not evidence.voice_certain   # speech the check cannot attribute
        verified_silence = fresh and evidence.voice_certain and not evidence.speech_detected and not evidence.voice_ids
        if voice:
            self.voice_streak, self.voice_seen = min(self.voice_streak + 1, Gate.REQUIRED_CHECKS), now
        elif other_voice or unknown_voice or (fresh and not verified_silence):
            self.voice_streak = 0           # an uncertain second is not a pause
        elif now - self.voice_seen > self.GRACE:
            self.voice_streak = 0           # only verified silence or a capture gap keeps the count
        self.streak = max(self.face_streak, self.voice_streak)
        if fresh and self.streak >= Gate.REQUIRED_CHECKS:
            self.allowed, self.owner_seen, self.closed_reason = True, now, None
            self.confirmed_by = "voice" if self.voice_streak >= Gate.REQUIRED_CHECKS else "face"
            self.generation += 1
        return self.allowed

    def permits(self, now):
        if self.allowed and now - self.owner_seen > self.ABSENCE: self.invalidate("absence")
        return self.allowed


class Gate:
    CHECK_SECONDS = 1
    REQUIRED_CHECKS = 3
    MAX_AGE = 2.5

    def __init__(self, store):
        self.store = store
        self.enabled = False  # Process restart always requires three new checks.
        self.allowed = False
        self.streak = 0
        self.last_check = float("-inf")
        self.last_capture = float("-inf")
        self.generation = 0
        self.reason = "owner-paused"
        self.gap_start = None

    def _state(self, allowed, reason, now):
        reason_changed = self.reason != reason
        if self.allowed != allowed or self.reason != reason:
            self.generation += 1
        self.allowed, self.reason = allowed, reason
        if not allowed and reason_changed and self.gap_start is not None:
            self.store.execute("UPDATE events SET end=? WHERE end IS NULL AND source='all'", (now,))
            self.gap_start = None
        if not allowed and self.gap_start is None:
            self.gap_start = now
            self.store.event("all", reason, now)
        elif allowed and self.gap_start is not None:
            self.store.execute("UPDATE events SET end=? WHERE end IS NULL AND source='all'", (now,))
            self.gap_start = None

    def enable(self, enabled, now):
        self.enabled = enabled
        self.streak = 0
        self.last_check = self.last_capture = float("-inf")
        self._state(False, "permission-paused" if enabled else "owner-paused", now)

    def invalidate(self, now, reason="permission-paused"):
        self.streak = 0
        self._state(False, reason, now)

    def check(self, evidence: Evidence, now):
        agreements = {r["id"] for r in self.store.execute("SELECT id FROM agreements WHERE active=1")}
        valid = (self.enabled and evidence.camera_ok and evidence.microphone_ok and evidence.certain
                 and bool(evidence.verified) and evidence.verified <= agreements
                 and 0 <= now - evidence.captured_at <= self.MAX_AGE
                 and evidence.captured_at > self.last_capture
                 and now - self.last_check <= self.MAX_AGE)
        # The first valid sample starts a new streak even after a long pause.
        if self.last_check == float("-inf") or now - self.last_check > self.MAX_AGE:
            self.streak = 0
            valid = (self.enabled and evidence.camera_ok and evidence.microphone_ok and evidence.certain
                     and bool(evidence.verified) and evidence.verified <= agreements
                     and 0 <= now - evidence.captured_at <= self.MAX_AGE
                     and evidence.captured_at > self.last_capture)
        self.last_check = now
        self.last_capture = max(self.last_capture, evidence.captured_at)
        self.streak = self.streak + 1 if valid else 0
        self._state(valid and self.streak >= self.REQUIRED_CHECKS,
                    "active" if valid and self.streak >= self.REQUIRED_CHECKS else
                    ("permission-paused" if self.enabled else "owner-paused"), now)
        return self.allowed

    def permits(self, now):
        if self.allowed and (not 0 <= now-self.last_check <= self.MAX_AGE
                or not 0 <= now-self.last_capture <= self.MAX_AGE):
            self.invalidate(now, "stale-participant-check")
        return self.enabled and self.allowed

    def status(self, now):
        self.permits(now)
        return {"enabled": self.enabled, "recording_allowed": self.allowed,
                "reason": self.reason, "gap_start": self.gap_start}
