"""Dialogue state, scene cues and turn judgements (feature butler-conversational-skill, DEC-001).

Pure data and rules, no I/O: the conversation module feeds it transcripts,
timestamps and face geometry and gets back what to do. Everything here is
metadata; no transcript is stored beyond the question Butler itself asked.
"""
from __future__ import annotations

import math
import re
import statistics
import uuid
from dataclasses import dataclass, field
from typing import Optional

SILENCE_DEFAULT = 600.0       # «помолчи» without a duration
SILENCE_CALL = 2 * 3600.0     # «пока я на звонке»: bounded even if the call never ends
FACING_LIMIT = 0.35           # |nose offset / eye distance| above this counts as turned away
LIP_MOTION_LIMIT = 0.06       # mouth-ratio spread across recent frames above this counts as talking

BACKCHANNEL = re.compile(r"^\W*(?:угу|ага|да|да-да|ясно|понятно|ок|окей|хорошо|так|продолжай|дальше|mm+|mhm|uh-?huh|"
                         r"yes|ok|okay|right|go on)\W*$", re.I)
UNFINISHED_TAIL = re.compile(r"(?:[,:;…-]|\b(?:и|а|но|или|что|чтобы|потом|затем|когда|если|в|на|к|с|по|за|для|про|"
                             r"and|or|but|that|then|to|at|in|on|for|with)\s*)$", re.I)
SILENCE_DURATION = re.compile(r"(?:на|минут[ы]?\s*)?(\d{1,3})\s*(минут\w*|мин\.?|час\w*|ч\.?)", re.I)
SILENCE_CALL_WORDS = re.compile(r"пока\s+я\s+(?:на\s+(?:звонке|созвоне|встрече|митинге)|говорю|разговариваю)", re.I)
SILENCE_HOUR_WORDS = re.compile(r"\b(?:на\s+)?(?:час|часик)\b", re.I)
OUTCOMES = ("silent", "repeat", "wait", "revise", "answer")


# ----------------------------------------------------------------------------- session

@dataclass
class Session:
    """With whom Butler is talking, what it asked, and whether it was told to be quiet."""

    partner: Optional[str] = None
    started_at: float = float("-inf")
    last_at: float = float("-inf")
    question: Optional[str] = None
    question_at: float = float("-inf")
    expects: Optional[str] = None
    question_id: Optional[str] = None
    question_task: Optional[str] = None
    proposal: Optional[str] = None
    quiet_until: float = float("-inf")
    quiet_scope: Optional[str] = None
    quiet_resume: Optional[str] = None

    @property
    def active(self) -> bool:
        return math.isfinite(self.last_at)

    def touch(self, partner: str, now: float) -> None:
        if not self.active or self.partner != partner:
            self.started_at = now
        self.partner, self.last_at = partner, now

    def ask(self, question: str, now: float, expects: Optional[str] = None, *, task=None, proposal=None) -> None:
        self.touch('owner', now)
        self.question, self.question_at, self.expects = question[:1200], now, expects
        self.question_id, self.question_task, self.proposal = uuid.uuid4().hex, task, proposal

    def answered(self) -> None:
        self.question, self.question_at, self.expects = None, float("-inf"), None
        self.question_id = self.question_task = self.proposal = None

    def answering(self, partner: str, now: float) -> bool:
        """The pending question survives ordinary pauses; action permission is separate."""
        return self.question is not None and self.partner == partner and now >= self.question_at

    def silence(self, now: float, until: float, scope: str, resume: str) -> None:
        self.quiet_until, self.quiet_scope, self.quiet_resume = until, scope, resume

    def quiet(self, now: float) -> bool:
        return now < self.quiet_until

    def resume(self, reason: str = "name") -> None:
        self.quiet_until, self.quiet_scope, self.quiet_resume = float("-inf"), None, reason

    def end(self) -> None:
        self.partner, self.started_at, self.last_at = None, float("-inf"), float("-inf")
        self.answered()

    def snapshot(self, now: float) -> dict:
        return {"partner": self.partner, "active": self.active,
                "since_last_s": None if not self.active else round(now - self.last_at, 1),
                "pending_question": self.question is not None, "expects": self.expects,
                "question_id": self.question_id, "question_task": self.question_task,
                "quiet": self.quiet(now), "quiet_for_s": max(0, round(self.quiet_until - now)) if self.quiet(now) else 0,
                "quiet_scope": self.quiet_scope if self.quiet(now) else None}


def parse_silence(text: str, now: float) -> tuple[float, str, str]:
    """How long «помолчи» lasts: a stated duration, the end of a call, or the default. The name always resumes."""
    if SILENCE_CALL_WORDS.search(text):
        return now + SILENCE_CALL, "until the call ends", "name or two hours"
    match = SILENCE_DURATION.search(text)
    if match:
        amount, unit = int(match.group(1)), match.group(2).lower()
        seconds = amount * (3600 if unit.startswith(("час", "ч")) else 60)
        return now + min(seconds, 12 * 3600), f"{amount} {unit}", "name or timer"
    if SILENCE_HOUR_WORDS.search(text):
        return now + 3600, "one hour", "name or timer"
    return now + SILENCE_DEFAULT, "ten minutes", "name or timer"


# ----------------------------------------------------------------------------- scene

@dataclass(frozen=True)
class Scene:
    """What the camera says about the people in front of the Mac for one batch. ``available`` False = no image."""

    available: bool = False
    faces: int = 0
    owner_visible: bool = False
    owner_facing: Optional[bool] = None
    owner_talking: Optional[bool] = None
    others_present: bool = False

    def data(self) -> dict:
        return {"image_available": self.available, "faces": self.faces, "owner_facing_mac": self.owner_facing,
                "owner_lips_moving": self.owner_talking, "other_people_present": self.others_present}


def face_geometry(face) -> dict:
    """YuNet row → box, head-orientation proxy and mouth ratio. Landmarks: right eye, left eye, nose, mouth corners."""
    x, y, w, h = (float(v) for v in face[:4])
    points = [(float(face[4 + 2 * i]), float(face[5 + 2 * i])) for i in range(5)]
    (rx, ry), (lx, ly), (nx, ny), (mrx, mry), (mlx, mly) = points
    eye_distance = max(1.0, math.hypot(lx - rx, ly - ry))
    eye_mid_x = (rx + lx) / 2
    yaw = (nx - eye_mid_x) / eye_distance
    mouth_ratio = math.hypot(mlx - mrx, mly - mry) / eye_distance
    return {"box": (x, y, w, h), "yaw": round(yaw, 3), "mouth_ratio": round(mouth_ratio, 3), "area": w * h}


class LipMotion:
    """Mouth-ratio history per identity; talking = the ratio keeps changing."""

    def __init__(self, keep: int = 6):
        self.keep, self.history = keep, {}

    def observe(self, pid: str, mouth_ratio: float) -> Optional[bool]:
        values = self.history.setdefault(pid, [])
        values.append(mouth_ratio)
        del values[:-self.keep]
        if len(values) < 3:
            return None
        return statistics.pstdev(values) > LIP_MOTION_LIMIT

    def forget(self, pid: str) -> None:
        self.history.pop(pid, None)


def scene_from(geometries: list, identities: dict, lips: Optional[LipMotion] = None) -> Scene:
    """Faces of one frame with their matched identity (index → pid, or None) → Scene."""
    if not geometries:
        return Scene(available=True, faces=0)
    owner_index = next((i for i, pid in identities.items() if pid == "owner"), None)
    others = any(pid != "owner" for i, pid in identities.items()) or len(geometries) > len(
        [i for i, pid in identities.items() if pid == "owner"])
    if owner_index is None:
        return Scene(available=True, faces=len(geometries), owner_visible=False, others_present=len(geometries) > 0)
    owner = geometries[owner_index]
    facing = abs(owner["yaw"]) <= FACING_LIMIT
    talking = lips.observe("owner", owner["mouth_ratio"]) if lips is not None else None
    return Scene(available=True, faces=len(geometries), owner_visible=True, owner_facing=facing, owner_talking=talking,
                 others_present=others)


class SceneAggregate:
    """The scene over one utterance: fractions, so a single bad frame does not decide."""

    def __init__(self):
        self.samples = []

    def clear(self) -> None:
        self.samples = []

    def add(self, scene: Scene) -> None:
        if scene.available:
            self.samples.append(scene)
            del self.samples[:-20]

    def summary(self) -> Scene:
        if not self.samples:
            return Scene()
        facing = [s.owner_facing for s in self.samples if s.owner_facing is not None]
        talking = [s.owner_talking for s in self.samples if s.owner_talking is not None]
        return Scene(available=True, faces=max(s.faces for s in self.samples),
                     owner_visible=any(s.owner_visible for s in self.samples),
                     owner_facing=None if not facing else sum(facing) / len(facing) >= 0.5,
                     owner_talking=None if not talking else sum(talking) / len(talking) >= 0.5,
                     others_present=sum(s.others_present for s in self.samples) / len(self.samples) >= 0.3)


# ----------------------------------------------------------------------------- judgements

def unfinished(text: str) -> bool:
    """A thought that stopped mid-way: trailing conjunction, preposition, comma or ellipsis."""
    stripped = text.strip()
    return bool(stripped) and bool(UNFINISHED_TAIL.search(stripped))


def backchannel(text: str) -> bool:
    return bool(BACKCHANNEL.match(text.strip())) and len(text.strip()) <= 24


def unintelligible(text: str, aliases) -> bool:
    """Clearly addressed but nothing to act on: the name alone or one short word after it."""
    words = re.findall(r"[\w-]+", text.casefold())
    rest = [w for w in words if w not in {a.casefold() for a in aliases}]
    return len(rest) <= 1 and sum(len(w) for w in rest) <= 4


def outcome(*, addressed: bool, understood: bool, may_speak: bool, waiting: bool = False, superseded: bool = False) -> str:
    """The five outcomes of REQ-033, from the three separate judgements."""
    if superseded:
        return "revise"
    if waiting:
        return "wait"
    if not addressed:
        return "silent"
    if not understood:
        return "repeat"
    if not may_speak:
        return "wait"
    return "answer"


@dataclass
class TurnMetrics:
    """Counters the verification report reads (REQ-035): interventions, missed addresses, stop latency."""

    outcomes: dict = field(default_factory=dict)
    stop_latencies: list = field(default_factory=list)
    corrections: int = 0          # owner said «я тебе говорю» or repeated with the name right after a silence
    interventions: int = 0        # replies the owner cut off with «я не тебе» / «не отвечай»
    vision_used: int = 0
    backchannels: int = 0

    def record(self, value: str, vision: bool = False) -> None:
        self.outcomes[value] = self.outcomes.get(value, 0) + 1
        if vision:
            self.vision_used += 1

    def stopped(self, latency: float) -> None:
        self.stop_latencies.append(round(latency, 3))
        del self.stop_latencies[:-200]

    def snapshot(self) -> dict:
        latencies = sorted(self.stop_latencies)
        p = lambda q: None if not latencies else latencies[min(len(latencies) - 1, int(q * len(latencies)))]
        return {"outcomes": dict(self.outcomes), "stop_latency_p50": p(0.5), "stop_latency_p95": p(0.95),
                "interruptions": len(latencies), "corrections": self.corrections, "interventions": self.interventions,
                "vision_used": self.vision_used, "backchannels": self.backchannels}


CORRECTION = re.compile(r"\b(?:я\s+(?:тебе|к\s+тебе)|тебе\s+говорю|я\s+с\s+тобой)\b", re.I)
NOT_YOU = re.compile(r"\b(?:я\s+не\s+тебе|не\s+с\s+тобой|не\s+тебе)\b", re.I)
