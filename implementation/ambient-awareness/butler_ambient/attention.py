"""Attention: whom the owner is talking to (feature butler-attention-architecture, REQ-001/REQ-002).

Four states. IDLE: nobody is talking to Butler. ENGAGED: a Butler session, the
owner spoke with Butler within the last five minutes, so follow-ups are for him.
BUSY: the owner is in a conversation with someone else: a phone at the ear, a
second face or voice, an application using the microphone (a call, dictation to
an AI), a meeting application, or the owner talking while looking away from the
Mac for ten seconds. In BUSY Butler records as usual and speaks only when named
or asked to stop. PAUSED: Full Pause.

Every transition is kept with its cause (never with audio or words) for the
audit and the owner panel. The name clears BUSY at once.
"""
from __future__ import annotations

import io
import threading
import time
from collections import deque

IDLE, ENGAGED, BUSY, PAUSED = "idle", "engaged", "busy", "paused"
BUSY_HOLD = 30.0            # seconds BUSY outlives the last busy signal
SESSION = 300.0             # seconds a Butler session keeps follow-ups addressed
AWAY_TALKING = 10.0         # seconds of talking while looking away that mean "a call or a person off camera"
VISION_CACHE = 5.0          # seconds a vision judgement of the frame stays valid
DARK_LUMINANCE = 35         # mean luminance (0..255) below which the frame is not judged
VISION_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["phone_at_ear", "talking_to_person", "person_visible"],
                 "properties": {"phone_at_ear": {"type": "boolean"}, "talking_to_person": {"type": "boolean"},
                                "person_visible": {"type": "boolean"}}}
VISION_PROMPT = ("Look at the frame from a Mac's camera. Answer JSON only. phone_at_ear: a person holds a phone to the ear or "
                 "speaks into a phone. talking_to_person: the person in front is turned to and speaking with another person "
                 "in the frame. person_visible: at least one person is visible. Do not describe anything else.")
BUSY_CUE = __import__("re").compile(r"\b(?:я\s+(?:сейчас\s+)?(?:на\s+(?:звонке|телефоне|встрече|созвоне|связи)|разговариваю\s+по\s+телефону|занят[а]?)|не\s+мешай|у\s+меня\s+(?:звонок|встреча))\b", __import__("re").I)
FREE_CUE = __import__("re").compile(r"\b(?:я\s+(?:уже\s+)?(?:свободен|свободна|освободил[ас]я|закончил[а]?|договорил[а]?)|можно\s+говорить|звонок\s+закончен)\b", __import__("re").I)
MANUAL_BUSY = 600.0         # seconds «я на звонке» holds without any other signal
CAUSE_TEXT = {"owner-said": "вы сказали, что заняты", "other-voice": "слышен другой голос", "other-face": "рядом ещё человек", "looking-away-talking": "говорит, глядя в сторону",
              "mic-app": "микрофон занят другим приложением", "meeting": "режим встречи", "call-app": "окно звонка на переднем плане",
              "phone-at-ear": "телефон у уха", "talking-to-person": "разговор с человеком рядом"}


def luminance(jpeg):
    """Mean brightness of a frame, 0..255; None when it cannot be decoded. Only the number leaves this function."""
    try:
        from PIL import Image, ImageStat
        with Image.open(io.BytesIO(jpeg)) as image:
            return ImageStat.Stat(image.convert("L").resize((64, 36))).mean[0]
    except Exception:
        return None


class Attention:
    def __init__(self, service, *, clock=time.time):
        self.service, self.clock = service, clock
        self.lock = threading.Lock()
        self.busy_until = float("-inf")
        self.busy_cause = None
        self.engaged_until = float("-inf")
        self.free_until = float("-inf")     # «я свободен»: application and meeting signals are ignored for a while
        self.away_since = None
        self.transitions = deque(maxlen=50)
        self.last_state = IDLE
        self.last_cause = None
        self.vision = {"at": float("-inf"), "result": None}
        self.counts = {}
        self.last_company_at = float("-inf")

    # -- state -------------------------------------------------------------------------------

    def state(self, now=None):
        now = self.clock() if now is None else now
        gate = getattr(self.service, "gate", None)
        if gate is not None and getattr(gate, "enabled", True) is False:
            return PAUSED
        if now < self.busy_until:
            return BUSY
        if now < self.engaged_until:
            return ENGAGED
        return IDLE

    def _note(self, now, cause=None):
        state = self.state(now)
        if state != self.last_state or (state == BUSY and cause is not None and cause != self.last_cause):
            self.transitions.append({"at": now, "state": state, "cause": cause})
            calls = getattr(self.service, "calls", None)
            if calls is not None and hasattr(calls, "transition"):
                calls.transition(state, cause, now)                 # DEC-068: a busy episode may be a call to conclude
            changed_state, self.last_state, self.last_cause = state != self.last_state, state, cause
            if not changed_state:
                return
            voice = getattr(self.service, "voice", None)
            debug = getattr(voice, "_debug", None)
            if callable(debug):
                text = {BUSY: "Занят с другим — новое обращение по имени, продолжение проверяю. Причина: " + CAUSE_TEXT.get(cause, cause or ""),
                        ENGAGED: "Разговор со мной — отвечаю на продолжения без имени.",
                        IDLE: "Свободен.", PAUSED: "Пауза."}[state]
                debug("attention", text)

    def _busy(self, cause, now):
        self.counts[cause] = self.counts.get(cause, 0) + 1
        if cause in {"other-voice", "other-face", "talking-to-person"}:
            self.last_company_at = now
        self.busy_until, self.busy_cause = now + BUSY_HOLD, cause
        self._note(now, cause)

    # -- signals -------------------------------------------------------------------------------

    def observe(self, evidence, now=None):
        """One sensed second: who is around and what the owner is doing. Called for every batch."""
        now = self.clock() if now is None else now
        with self.lock:
            scene = getattr(evidence, "scene", None)
            if evidence.voice_certain and evidence.voice_ids and evidence.voice_ids != frozenset({"owner"}):
                self._busy("other-voice", now)
            # One face the check cannot identify is most often the owner in bad light; two faces are company.
            if scene is not None and scene.available and scene.others_present and scene.faces >= 2:
                self._busy("other-face", now)
            if scene is not None and scene.available and scene.owner_visible and scene.owner_facing is False and scene.owner_talking:
                self.away_since = now if self.away_since is None else self.away_since
                if now - self.away_since >= AWAY_TALKING:
                    self._busy("looking-away-talking", now)
            else:
                self.away_since = None
            meeting = getattr(self.service, "meeting", None)
            if meeting is not None and getattr(meeting, "quiet", False) and now >= self.free_until:
                state = getattr(meeting, "state", "")
                if state == "external-microphone-active":
                    self._busy("mic-app", now)
                elif state in {"manual-meeting", "meeting-release-delay"}:
                    self._busy("meeting", now)
            self._note(now)

    def desktop(self, desktop, now=None):
        """The desktop state sensed at turn time: a call application in front means busy."""
        now = self.clock() if now is None else now
        with self.lock:
            if desktop.get("call_app") is True and now >= self.free_until:
                self._busy("call-app", now)

    def look(self, now=None):
        """Judge the current camera frame locally: a phone at the ear or a conversation with someone in the frame.

        Runs at most once per VISION_CACHE seconds and never on a dark frame. The frame and the answer stay local;
        only the booleans are kept."""
        now = self.clock() if now is None else now
        with self.lock:
            if now - self.vision["at"] <= VISION_CACHE:
                return self.vision["result"]
            frame = getattr(self.service, "preview_frame", None)
        if not frame or not 0 <= now - frame[1] <= 3.0:
            return None
        jpeg = frame[0]
        level = luminance(jpeg)
        if level is None or level < DARK_LUMINANCE:
            with self.lock:
                self.vision = {"at": now, "result": None}
            return None
        model = getattr(getattr(self.service, "voice", None), "vision_model", None)
        if model is None:
            return None
        try:
            verdict = model.generate_json(VISION_PROMPT, VISION_SCHEMA, images=[jpeg], options={"num_predict": 60})
        except Exception:
            self.counts["vision-failed"] = self.counts.get("vision-failed", 0) + 1
            return None
        result = {k: bool(verdict.get(k)) for k in ("phone_at_ear", "talking_to_person", "person_visible")}
        # One frame cannot tell "talking to the camera" from "talking to someone": with a single face the owner
        # alone was judged to be in conversation (2026-10-25) and lost his follow-ups. The face detector must
        # corroborate: company means at least two faces in the current scene.
        scene = getattr(self.service, "latest_scene", None)
        faces = getattr(scene, "faces", 0) if scene is not None and getattr(scene, "available", False) else 0
        with self.lock:
            self.vision = {"at": now, "result": result}
            if result["phone_at_ear"]:
                self._busy("phone-at-ear", now)
            elif result["talking_to_person"] and faces >= 2:
                self._busy("talking-to-person", now)
        return result

    # -- the conversation tells the state machine what happened ---------------------------------

    def spoken_cue(self, text, now=None):
        """«Я на звонке» / «я свободен»: the owner sets or clears busy by hand. Returns 'busy', 'free' or None."""
        now = self.clock() if now is None else now
        if FREE_CUE.search(text):
            with self.lock:
                self.busy_until, self.busy_cause = float("-inf"), None
                # A browser tab or a call application that keeps the microphone open kept Butler «busy» for an
                # afternoon (2026-10-25: 221 unanswered utterances). The owner's word outranks those signals for a while.
                self.free_until = now + MANUAL_BUSY
                self._note(now, "owner-free")
            return "free"
        if BUSY_CUE.search(text):
            with self.lock:
                self.counts["owner-said"] = self.counts.get("owner-said", 0) + 1
                self.busy_until, self.busy_cause = now + MANUAL_BUSY, "owner-said"
                self._note(now, "owner-said")
            return "busy"
        return None

    def named(self, now=None):
        """The name opens a turn at once, whatever the owner was doing."""
        now = self.clock() if now is None else now
        with self.lock:
            # DEC-072: naming Butler opens a session, but does not end a call.
            # Subsequent unnamed speech still needs positive attribution.
            self.engaged_until = now + SESSION
            self._note(now, "named")

    def engaged(self, now=None):
        """A turn was answered: follow-ups for the next five minutes are for Butler."""
        now = self.clock() if now is None else now
        with self.lock:
            self.engaged_until = now + SESSION
            self._note(now, "answered")

    def ended(self, now=None):
        """The owner ended the session («помолчи», a stop) or the private lease was released."""
        now = self.clock() if now is None else now
        with self.lock:
            self.engaged_until = float("-inf")
            self._note(now, "ended")

    def company_recent(self, now=None, window=60.0):
        """Someone else was heard or seen within the window: second-person speech may be for them."""
        now = self.clock() if now is None else now
        return now - self.last_company_at <= window

    def snapshot(self, now=None):
        now = self.clock() if now is None else now
        with self.lock:
            state = self.state(now)
            return {"state": state, "busy_cause": self.busy_cause if state == BUSY else None,
                "free_until": self.free_until if now < self.free_until else None,
                    "busy_for_s": round(self.busy_until - now, 1) if state == BUSY else 0,
                    "session_for_s": round(self.engaged_until - now, 1) if now < self.engaged_until else 0,
                    "transitions": list(self.transitions)[-10:], "signals": dict(self.counts),
                    "vision": self.vision["result"], "rule": "busy: new session by name; follow-ups require positive local attribution"}
