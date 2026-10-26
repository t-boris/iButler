"""Deterministic news/deep-research video renderer for AI Butler.

The canonical input is an evidence-linked episode JSON document. Rendering is
offline by default: Pillow creates cards and burned-in caption layers, macOS
``say`` (or silence) supplies narration, and FFmpeg assembles H.264/AAC MP4.

Veo is deliberately isolated behind an explicit paid-API approval gate. It may
only generate decorative intro/outro B-roll; a missing/rejected clip falls back
to the normal graphic card without failing the episode.
"""
from __future__ import annotations

import contextlib
import dataclasses
import datetime as dt
import hashlib
import json
import math
import fcntl
import os
import random
import re
import shutil
import subprocess
import tempfile
import textwrap
import time
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps


SCHEMA_VERSION = "butler.news-video/v1"
DEFAULT_WORKSPACE = Path("/Users/boris/.openclaw/workspace-butler")
RUNS_ROOT = Path(os.environ.get("BUTLER_NEWS_VIDEO_RUNS", DEFAULT_WORKSPACE / "runs" / "news-video"))

FFMPEG = "/opt/homebrew/bin/ffmpeg"
FFPROBE = "/opt/homebrew/bin/ffprobe"
SAY = "/usr/bin/say"
PYTHON = "/opt/miniconda3/bin/python"
OPENAI_TTS_CLI = "/Users/boris/.codex/skills/speech/scripts/text_to_speech.py"

ALLOWED_TTS_PROVIDERS = {"edge", "macos", "openai", "none"}
# Free Microsoft neural voices via edge-tts; no API key and no billing involved.
EDGE_TTS_DEFAULT_VOICE = "ru-RU-DmitryNeural"
EDGE_TTS_BASELINE_WPM = 175
MACOS_FALLBACK_VOICE = "Milena"
DEFAULT_TTS_VOICES = {"edge": EDGE_TTS_DEFAULT_VOICE, "macos": MACOS_FALLBACK_VOICE, "openai": "cedar"}
OPENCLAW = "/opt/homebrew/bin/openclaw"
# The gateway refuses outbound local media from workspace-* directories; only
# state-level roots such as ~/.openclaw/media are allowed, so publish stages
# artifacts there before sending.
OPENCLAW_MEDIA_STAGING = Path("/Users/boris/.openclaw/media/news-video")
BUTLER_TIMEZONE = "America/Chicago"

FONT_REGULAR = "/System/Library/Fonts/Supplemental/Arial.ttf"
FONT_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"

# Boris 2026-07-20: «Ускорь скорость речи на 1.3» — every synthesized speech part
# is tempo-shifted (pitch-preserving) on conversion; pauses/silence stay
# real-time. A profile may override it: Bamberg News speaks at 1.0 for a TV
# audience (Boris 2026-08-01).
TTS_TEMPO = 1.3

# Portrait 9:16 is the historical default (phone-first delivery). A profile may
# override it — Bamberg News is watched on a TV (Boris 2026-08-01).
DEFAULT_FORMAT: dict[str, int] = {"width": 1080, "height": 1920, "fps": 30}
LANDSCAPE_FORMAT: dict[str, int] = {"width": 1920, "height": 1080, "fps": 30}

PROFILE_RULES: dict[str, dict[str, Any]] = {
    "daily": {
        "max_factual_scenes": 10,
        "target_seconds": (45, 180),
        "default_title": "Главное к этому часу",
    },
    # Bamberg News tiered edition (Boris 2026-07-20): 1-7 stories per
    # geographic tier plus medicine/science, so episodes run longer.
    # Landscape 16:9 and real-time speech (Boris 2026-08-01): the edition is
    # watched on a television, where 9:16 and 1.3x speech do not work.
    "bamberg-daily": {
        "max_factual_scenes": 28,
        "target_seconds": (120, 480),
        "default_title": "Бамберг: главное к этому часу",
        "format": LANDSCAPE_FORMAT,
        "tts_tempo": 1.0,
    },
    # Boris's own tiered edition (2026-07-20): local→state→US→world plus
    # AI/swdev/science/business/markets/crypto/ukraine sections.
    "butler-daily": {
        "max_factual_scenes": 30,
        "target_seconds": (150, 600),
        "default_title": "Главное к этому часу",
    },
    "deep-research": {
        "max_factual_scenes": 12,
        "target_seconds": (60, 420),
        "default_title": "Deep Research",
    },
    # On-demand explanatory lectures built from the knowledge base: a topic,
    # one report, or a whole vault folder. 10-20 minutes of narration.
    "lecture": {
        "max_factual_scenes": 80,
        "target_seconds": (540, 1260),
        "default_title": "Лекция",
    },
}

ALLOWED_SCENE_KINDS = {"intro", "story", "finding", "context", "outro"}
FACTUAL_SCENE_KINDS = {"story", "finding"}
ALLOWED_STATUSES = {"confirmed", "developing", "context", "not-applicable"}
ALLOWED_VISUAL_MODES = {"template", "image", "veo", "broll"}
BROLL_SUFFIXES = {".mp4", ".mov", ".m4v", ".webm", ".jpg", ".jpeg", ".png", ".webp"}
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
# How a still fills its media window. "cover" crops to fill (Ken Burns motion);
# "contain" keeps the whole image visible — mandatory for maps, charts and any
# other informational graphic (Boris 2026-08-01: «карту показывать полностью»).
ALLOWED_VISUAL_FITS = {"cover", "contain"}
MAX_SLIDESHOW_IMAGES = 4
# A slide shorter than this reads as a flicker rather than a picture change.
MIN_SLIDE_SECONDS = 2.4
SLIDE_CROSSFADE_SECONDS = 0.7

NARRATION_MARKER_RE = re.compile(r"\[(pause|breath|beat)(?::([0-9]*\.?[0-9]+))?\]", re.IGNORECASE)
NARRATION_SENTENCE_RE = re.compile(r"(?<=[.!?…])\s+")
NARRATION_PARAGRAPH_RE = re.compile(r"\n\s*\n+")

DEFAULT_MARKER_PAUSES = {
    "pause": 0.35,
    "breath": 0.28,
    "beat": 0.18,
}

SCENE_THEME_RULES: list[tuple[tuple[str, ...], str, str, str]] = [
    (("ai", "gpt", "gemini", "claude", "model", "codex", "cursor", "copilot", "openai"), "AI / software", "chip", "#60A5FA"),
    (("market", "stocks", "stock", "fed", "rates", "inflation", "economy", "finance", "bank", "bond", "oil", "brent"), "Markets / economy", "chart", "#F59E0B"),
    (("politic", "election", "congress", "senate", "government", "trump", "biden", "putin", "zelensky", "parliament", "policy"), "Politics / policy", "scales", "#A78BFA"),
    (("war", "military", "missile", "drone", "attack", "conflict", "front", "defense", "security"), "Conflict / security", "shield", "#EF4444"),
    (("court", "judge", "law", "legal", "justice", "supreme", "ruling", "regulation"), "Law / regulation", "gavel", "#F472B6"),
    (("science", "research", "study", "biology", "physics", "chemistry", "medical", "health", "vaccine"), "Science / health", "atom", "#22C55E"),
    (("space", "rocket", "satellite", "nasa", "orbital"), "Space / exploration", "rocket", "#38BDF8"),
    (("energy", "climate", "weather", "power", "electric", "grid", "solar", "wind"), "Energy / climate", "bolt", "#34D399"),
    (("transport", "airline", "plane", "ship", "rail", "car", "traffic", "travel"), "Transport / mobility", "plane", "#FB7185"),
    (("product", "launch", "release", "feature", "update", "platform", "app", "service"), "Product / launch", "panel", "#F97316"),
]

# Public Gemini Developer API list prices checked 2026-07-17. Rates vary by
# resolution: https://ai.google.dev/gemini-api/docs/pricing
VEO_COST_PER_SECOND: dict[str, dict[str, float]] = {
    "veo-3.1-lite-generate-preview": {"720p": 0.05, "1080p": 0.08},
    "veo-3.1-fast-generate-preview": {"720p": 0.10, "1080p": 0.12, "4k": 0.30},
    "veo-3.1-generate-preview": {"720p": 0.40, "1080p": 0.40, "4k": 0.60},
}
# Conservative authorization estimate, not a billing statement. The live
# OpenAI key is currently absent; this exists so a future opt-in cannot make a
# sequence of per-scene calls whose aggregate exceeds the caller's cap.
OPENAI_TTS_ESTIMATED_USD_PER_MINUTE = 0.06
RUN_MARKER = ".butler-news-video-run.json"


class NewsVideoError(RuntimeError):
    """Base news-video error."""


class ValidationError(NewsVideoError):
    """Episode JSON failed structural or evidence validation."""


class RenderError(NewsVideoError):
    """A deterministic render stage failed."""


class PaidAPIApprovalRequired(NewsVideoError):
    """A paid API stage was requested without explicit cost authorization."""


class DeliveryApprovalRequired(NewsVideoError):
    """An external delivery was requested without an explicit authorization."""


@dataclasses.dataclass(frozen=True)
class VeoRequest:
    scene_id: str
    prompt: str
    duration_seconds: int
    model: str
    aspect_ratio: str
    resolution: str
    estimated_cost_usd: float


def _now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temp, path)


@contextlib.contextmanager
def _locked_run_dir(run_dir: Path):
    lock_path = run_dir.parent / f".{run_dir.name}.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _episode_digest(episode: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(episode).encode("utf-8")).hexdigest()


def _paid_budget_authorized(
    *,
    stage: str,
    approval: str | None,
    max_usd: float | None,
    estimate_usd: float,
) -> tuple[str, float]:
    approval_text = str(approval or "").strip()
    try:
        cap = float(max_usd) if max_usd is not None else float("nan")
        estimate = float(estimate_usd)
    except (TypeError, ValueError) as exc:
        raise PaidAPIApprovalRequired(f"{stage}: invalid cost authorization; no API call was made") from exc
    if (
        not approval_text
        or len(approval_text) > 200
        or _has_control_chars(approval_text)
        or not math.isfinite(cap)
        or cap <= 0
        or not math.isfinite(estimate)
        or estimate < 0
        or cap + 1e-9 < estimate
    ):
        raise PaidAPIApprovalRequired(
            f"{stage} estimate is ${estimate:.2f}. Pass a valid --approve-paid-api reference "
            f"and a finite --max-usd >= {estimate:.2f}. No API call was made."
        )
    return approval_text, cap


def _safe_id(value: str, field: str = "id") -> str:
    value = str(value or "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,95}", value):
        raise ValidationError(f"{field} must match [a-z0-9][a-z0-9._-]{{0,95}}: {value!r}")
    return value


def _profile_rules(episode: dict[str, Any]) -> dict[str, Any]:
    return PROFILE_RULES.get(str(episode.get("profile", "")), {}) or {}


def episode_format(episode: dict[str, Any]) -> tuple[int, int, int]:
    """Frame geometry: explicit ``episode.format`` wins, then the profile default.

    Raises ValueError/TypeError on malformed values so callers that validate can
    report them; render paths only see formats that already passed validation.
    """
    default = _profile_rules(episode).get("format") or DEFAULT_FORMAT
    fmt = episode.get("format")
    fmt = fmt if isinstance(fmt, dict) else {}
    return (
        int(fmt.get("width", default["width"])),
        int(fmt.get("height", default["height"])),
        int(fmt.get("fps", default["fps"])),
    )


def episode_tempo(episode: dict[str, Any]) -> float:
    """Speech tempo multiplier: ``episode.audio.tempo`` wins, then the profile."""
    audio = episode.get("audio")
    if isinstance(audio, dict) and audio.get("tempo") is not None:
        try:
            tempo = float(audio["tempo"])
        except (TypeError, ValueError):
            tempo = 0.0
        if 0.5 <= tempo <= 2.0:
            return tempo
    return float(_profile_rules(episode).get("tts_tempo", TTS_TEMPO))


def deterministic_episode_id(episode: dict[str, Any]) -> str:
    """Stable ID for scheduler idempotency when an explicit ID is omitted."""
    raw_sources = episode.get("sources", [])
    safe_sources = raw_sources if isinstance(raw_sources, list) else []
    seed = {
        "profile": episode.get("profile"),
        "as_of": episode.get("as_of"),
        "window": episode.get("window", {}),
        "title": episode.get("title"),
        "source_ids": sorted(str(source.get("id", "")) for source in safe_sources if isinstance(source, dict)),
    }
    digest = hashlib.sha256(_canonical_json(seed).encode()).hexdigest()[:10]
    date = str(episode.get("as_of", "undated"))[:10].replace("-", "")
    profile = re.sub(r"[^a-z0-9-]", "-", str(episode.get("profile", "episode")).lower())
    return f"{profile}-{date}-{digest}"


def load_episode(path: str | Path) -> tuple[dict[str, Any], Path]:
    episode_path = Path(path).expanduser().resolve()
    try:
        raw = json.loads(episode_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationError(f"episode file not found: {episode_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationError(f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}") from exc
    if not isinstance(raw, dict):
        raise ValidationError("episode root must be a JSON object")
    if not raw.get("episode_id"):
        raw["episode_id"] = deterministic_episode_id(raw)
    return raw, episode_path


def _has_control_chars(value: str) -> bool:
    return bool(re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", value))


def _walk_strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_strings(item)


def _walk_numbers(value: Any) -> Iterable[float]:
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        yield float(value)
    elif isinstance(value, dict):
        for item in value.values():
            yield from _walk_numbers(item)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_numbers(item)


def _parse_iso(value: str, field: str) -> dt.datetime:
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"{field} must be ISO-8601: {value!r}") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{field} must include a timezone: {value!r}")
    return parsed


def _resolve_local_asset(base_dir: Path, relative_path: str, field: str) -> Path:
    candidate = Path(str(relative_path or ""))
    if candidate.is_absolute():
        raise ValidationError(f"{field} must be relative to episode.json, not absolute")
    resolved = (base_dir / candidate).resolve()
    try:
        resolved.relative_to(base_dir.resolve())
    except ValueError as exc:
        raise ValidationError(f"{field} escapes the episode directory: {relative_path!r}") from exc
    return resolved


def estimate_narration_seconds(text: str, words_per_minute: int = 175) -> float:
    words = len(re.findall(r"\w+", text, flags=re.UNICODE))
    punctuation_pause = 0.14 * len(re.findall(r"[.!?;:]", text))
    marker_pause = 0.0
    for marker, value in NARRATION_MARKER_RE.findall(text):
        if value:
            try:
                marker_pause += max(0.0, min(float(value), 3.0))
            except ValueError:
                marker_pause += DEFAULT_MARKER_PAUSES.get(marker.lower(), 0.0)
        else:
            marker_pause += DEFAULT_MARKER_PAUSES.get(marker.lower(), 0.0)
    paragraph_pause = 0.24 * max(0, len(NARRATION_PARAGRAPH_RE.findall(text)))
    return max(0.0, words / max(words_per_minute, 1) * 60.0 + punctuation_pause + marker_pause + paragraph_pause)


def _display_narration(text: str) -> str:
    """Narration as shown to the viewer: pause/breath markers are voice-only."""
    cleaned = NARRATION_MARKER_RE.sub(" ", str(text or ""))
    return re.sub(r"\s+", " ", cleaned).strip()


def _narration_script(text: str) -> list[dict[str, Any]]:
    value = str(text or "").replace("\r\n", "\n").strip()
    if not value:
        return []
    parts: list[dict[str, Any]] = []

    def push_text(chunk: str) -> None:
        cleaned = re.sub(r"\s+", " ", chunk).strip(" \n\t")
        if not cleaned:
            return
        sentences = [sentence.strip() for sentence in NARRATION_SENTENCE_RE.split(cleaned) if sentence.strip()]
        if not sentences:
            return
        for index, sentence in enumerate(sentences):
            parts.append({"kind": "text", "text": sentence})
            if index < len(sentences) - 1:
                tail = sentence[-1]
                parts.append({"kind": "pause", "seconds": 0.12 if tail in ".!?" else 0.08})

    pos = 0
    for match in NARRATION_MARKER_RE.finditer(value):
        push_text(value[pos:match.start()])
        marker = match.group(1).lower()
        raw = match.group(2)
        if raw:
            try:
                seconds = max(0.0, min(float(raw), 3.0))
            except ValueError:
                seconds = DEFAULT_MARKER_PAUSES.get(marker, 0.0)
        else:
            seconds = DEFAULT_MARKER_PAUSES.get(marker, 0.0)
        parts.append({"kind": "pause", "seconds": seconds})
        pos = match.end()
    push_text(value[pos:])

    collapsed: list[dict[str, Any]] = []
    for part in parts:
        if part["kind"] == "pause" and part.get("seconds", 0.0) <= 0:
            continue
        if collapsed and part["kind"] == "pause" and collapsed[-1]["kind"] == "pause":
            collapsed[-1]["seconds"] = min(float(collapsed[-1]["seconds"]) + float(part["seconds"]), 3.0)
            continue
        collapsed.append(part)
    if collapsed and collapsed[-1]["kind"] == "pause":
        collapsed.pop()
    return collapsed


def _write_audio_concat(parts: list[Path], out_path: Path) -> None:
    if not parts:
        _silence(out_path, 0.75)
        return
    concat_file = out_path.with_suffix(".concat.txt")
    lines = ["file '" + str(path).replace("'", "'\\''") + "'" for path in parts]
    concat_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temp_out = out_path.with_suffix(".tmp.wav")
    _run(
        [
            FFMPEG,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c",
            "copy",
            str(temp_out),
        ],
        timeout=180,
    )
    os.replace(temp_out, out_path)


def _validate_diagram(scene_id: str, diagram: Any, errors: list[str]) -> None:
    prefix = f"scene {scene_id}: visual.diagram"
    if not isinstance(diagram, dict):
        errors.append(f"{prefix} must be an object")
        return
    kind = diagram.get("type")
    if kind not in DIAGRAM_TYPES:
        errors.append(f"{prefix}.type must be one of {sorted(DIAGRAM_TYPES)}")
        return
    if kind == "weather":
        days = diagram.get("days")
        if not isinstance(days, list) or not 1 <= len(days) <= 4:
            errors.append(f"{prefix}.days must be a list of 1-4 day objects")
            return
        for index, day in enumerate(days):
            if not isinstance(day, dict) or not str(day.get("label", "")).strip():
                errors.append(f"{prefix}.days[{index}] needs a label")
                continue
            for field in ("min", "max"):
                value = day.get(field)
                if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
                    errors.append(f"{prefix}.days[{index}].{field} must be a finite number")
        current = diagram.get("current")
        if current is not None and not isinstance(current, dict):
            errors.append(f"{prefix}.current must be an object when present")
        return
    if kind == "market":
        items = diagram.get("items")
        if not isinstance(items, list) or not 1 <= len(items) <= 6:
            errors.append(f"{prefix}.items must be a list of 1-6 market rows")
            return
        for index, row in enumerate(items):
            if not isinstance(row, dict) or not str(row.get("label", "")).strip():
                errors.append(f"{prefix}.items[{index}] needs a label")
                continue
            if not str(row.get("value", "")).strip():
                errors.append(f"{prefix}.items[{index}] needs a value (price/level as shown)")
            change = row.get("change_pct")
            if change is not None and (isinstance(change, bool) or not isinstance(change, (int, float)) or not math.isfinite(float(change))):
                errors.append(f"{prefix}.items[{index}].change_pct must be a finite number")
            spark = row.get("spark")
            if spark is not None:
                if not isinstance(spark, list) or not 2 <= len(spark) <= 24 or not all(
                    isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v)) for v in spark
                ):
                    errors.append(f"{prefix}.items[{index}].spark must be 2-24 finite numbers")
        return
    if len(str(diagram.get("unit", ""))) > 8:
        errors.append(f"{prefix}.unit must be at most 8 characters")

    def _check_value(value: Any, where: str) -> None:
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
            errors.append(f"{prefix}.{where}.value must be a finite number")

    def _check_entries(key: str, minimum: int, maximum: int, *, numeric: bool, label_limit: int) -> None:
        entries = diagram.get(key)
        if not isinstance(entries, list) or not (minimum <= len(entries) <= maximum):
            errors.append(f"{prefix}.{key} must be an array of {minimum}-{maximum} entries")
            return
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict) or not str(entry.get("label", "")).strip():
                errors.append(f"{prefix}.{key}[{index}] needs a non-empty label")
                continue
            if len(str(entry.get("label", ""))) > label_limit:
                errors.append(f"{prefix}.{key}[{index}].label must be at most {label_limit} characters")
            if numeric:
                _check_value(entry.get("value"), f"{key}[{index}]")
                if len(str(entry.get("display", ""))) > 14:
                    errors.append(f"{prefix}.{key}[{index}].display must be at most 14 characters")
            elif len(str(entry.get("detail", ""))) > 80:
                errors.append(f"{prefix}.{key}[{index}].detail must be at most 80 characters")

    if kind in {"bars", "donut"}:
        _check_entries("items", 2, 6, numeric=True, label_limit=40)
    elif kind == "line":
        _check_entries("points", 2, 10, numeric=True, label_limit=14)
    elif kind in {"timeline", "flow"}:
        _check_entries("steps", 2, 6, numeric=False, label_limit=44)
    else:  # compare
        for side_key in ("left", "right"):
            side = diagram.get(side_key)
            if not isinstance(side, dict) or not str(side.get("title", "")).strip():
                errors.append(f"{prefix}.{side_key} needs a non-empty title")
                continue
            if len(str(side.get("title", ""))) > 32:
                errors.append(f"{prefix}.{side_key}.title must be at most 32 characters")
            side_items = side.get("items")
            if not isinstance(side_items, list) or not (1 <= len(side_items) <= 4):
                errors.append(f"{prefix}.{side_key}.items must be an array of 1-4 strings")
                continue
            if any(not isinstance(item, str) or not item.strip() or len(item) > 44 for item in side_items):
                errors.append(f"{prefix}.{side_key}.items entries must be non-empty strings of at most 44 characters")


def validate_episode(
    episode: dict[str, Any],
    *,
    episode_path: str | Path | None = None,
    strict: bool = True,
) -> list[str]:
    """Validate structure, evidence links, assets and profile constraints.

    Returns non-fatal warnings. Raises ValidationError for anything that could
    make a factual or technical render misleading/unsafe.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if episode.get("schema_version") != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION!r}")
    profile = episode.get("profile")
    if not isinstance(profile, str) or profile not in PROFILE_RULES:
        errors.append(f"profile must be one of {sorted(PROFILE_RULES)}")
    try:
        _safe_id(episode.get("episode_id") or deterministic_episode_id(episode), "episode_id")
    except ValidationError as exc:
        errors.append(str(exc))

    for key in ("title", "language", "as_of"):
        if not str(episode.get(key, "")).strip():
            errors.append(f"missing non-empty field: {key}")
    if episode.get("as_of"):
        try:
            _parse_iso(str(episode["as_of"]), "as_of")
        except ValidationError as exc:
            errors.append(str(exc))

    window = episode.get("window")
    if not isinstance(window, dict):
        errors.append("window must be an object")
    else:
        try:
            start = _parse_iso(str(window.get("from", "")), "window.from")
            end = _parse_iso(str(window.get("to", "")), "window.to")
            if start >= end:
                errors.append("window.from must be earlier than window.to")
        except ValidationError as exc:
            errors.append(str(exc))

    fmt = episode.get("format") or {}
    if not isinstance(fmt, dict):
        errors.append("format must be an object")
        fmt = {}
    try:
        width, height, fps = episode_format(episode)
        if not (360 <= width <= 3840 and 360 <= height <= 3840):
            errors.append("format width/height must each be between 360 and 3840")
        if fps not in {12, 15, 24, 25, 30, 50, 60}:
            errors.append("format.fps must be one of 12,15,24,25,30,50,60")
    except (TypeError, ValueError):
        errors.append("format width/height/fps must be integers")

    audio = episode.get("audio")
    if audio is not None and not isinstance(audio, dict):
        errors.append("audio must be an object")
    elif isinstance(audio, dict) and audio.get("tempo") is not None:
        try:
            tempo = float(audio["tempo"])
        except (TypeError, ValueError):
            errors.append("audio.tempo must be a number")
        else:
            if not (0.5 <= tempo <= 2.0):
                errors.append("audio.tempo must be between 0.5 and 2.0")

    for text in _walk_strings(episode):
        if _has_control_chars(text):
            errors.append("episode contains forbidden control characters")
            break
    if any(not math.isfinite(number) for number in _walk_numbers(episode)):
        errors.append("episode contains a non-finite number (NaN or Infinity)")

    sources = episode.get("sources")
    if not isinstance(sources, list):
        errors.append("sources must be an array")
        sources = []
    source_ids: set[str] = set()
    canonical_urls: set[str] = set()
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            errors.append(f"sources[{index}] must be an object")
            continue
        try:
            sid = _safe_id(source.get("id"), f"sources[{index}].id")
        except ValidationError as exc:
            errors.append(str(exc))
            continue
        if sid in source_ids:
            errors.append(f"duplicate source id: {sid}")
        source_ids.add(sid)
        if not str(source.get("title", "")).strip():
            errors.append(f"source {sid} is missing title")
        url = str(source.get("url", "")).strip()
        if url:
            parsed = urlparse(url)
            if parsed.scheme not in {"https", "vault"}:
                errors.append(f"source {sid} URL must use https:// or vault://")
            canonical = url.rstrip("/")
            if canonical in canonical_urls:
                errors.append(f"duplicate canonical source URL: {canonical}")
            canonical_urls.add(canonical)
        else:
            warnings.append(f"source {sid} has no URL; credits will show title only")

    scenes = episode.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        errors.append("scenes must be a non-empty array")
        scenes = []
    scene_ids: set[str] = set()
    factual_count = 0
    narration_total = 0.0
    episode_dir = Path(episode_path).expanduser().resolve().parent if episode_path else None
    for index, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            errors.append(f"scenes[{index}] must be an object")
            continue
        try:
            scene_id = _safe_id(scene.get("id"), f"scenes[{index}].id")
        except ValidationError as exc:
            errors.append(str(exc))
            continue
        if scene_id in scene_ids:
            errors.append(f"duplicate scene id: {scene_id}")
        scene_ids.add(scene_id)
        kind = scene.get("kind")
        if not isinstance(kind, str) or kind not in ALLOWED_SCENE_KINDS:
            errors.append(f"scene {scene_id}: kind must be one of {sorted(ALLOWED_SCENE_KINDS)}")
        if not str(scene.get("headline", "")).strip():
            errors.append(f"scene {scene_id}: headline is required")
        narration = str(scene.get("narration", "")).strip()
        if len(narration) > 700:
            errors.append(f"scene {scene_id}: narration exceeds 700 characters; split the scene")
        try:
            duration_hint = float(scene.get("duration_hint", 0) or 0)
            if not math.isfinite(duration_hint) or not (0 <= duration_hint <= 90):
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"scene {scene_id}: duration_hint must be a finite number between 0 and 90")
            duration_hint = 0.0
        narration_total += max(duration_hint, estimate_narration_seconds(narration))
        status = scene.get("status", "not-applicable")
        if not isinstance(status, str) or status not in ALLOWED_STATUSES:
            errors.append(f"scene {scene_id}: invalid status {status!r}")
        refs = scene.get("source_ids", [])
        if not isinstance(refs, list):
            errors.append(f"scene {scene_id}: source_ids must be an array")
            refs = []
        invalid_refs = [ref for ref in refs if not isinstance(ref, str)]
        if invalid_refs:
            errors.append(f"scene {scene_id}: source_ids entries must be strings")
        missing_refs = [ref for ref in refs if isinstance(ref, str) and ref not in source_ids]
        if missing_refs:
            errors.append(f"scene {scene_id}: unknown source ids: {missing_refs}")
        if isinstance(kind, str) and kind in FACTUAL_SCENE_KINDS:
            factual_count += 1
            if not refs:
                errors.append(f"scene {scene_id}: factual scenes require at least one source_id")
            if strict and (not isinstance(status, str) or status not in {"confirmed", "developing"}):
                errors.append(f"scene {scene_id}: factual status must be confirmed or developing")
            if strict and status == "confirmed":
                trusted_sources = [
                    source.get("id")
                    for source in sources
                    if isinstance(source, dict)
                    and source.get("id") in refs
                    and str(source.get("trust_tier", "")).strip() in {"primary", "internal"}
                ]
                if not trusted_sources:
                    errors.append(
                        f"scene {scene_id}: confirmed scenes need at least one primary or internal source"
                    )
        visual = scene.get("visual") or {"mode": "template"}
        if not isinstance(visual, dict):
            errors.append(f"scene {scene_id}: visual must be an object")
            visual = {"mode": "template"}
        mode = visual.get("mode", "template")
        if not isinstance(mode, str) or mode not in ALLOWED_VISUAL_MODES:
            errors.append(f"scene {scene_id}: visual.mode must be one of {sorted(ALLOWED_VISUAL_MODES)}")
        if mode == "veo" and (not isinstance(kind, str) or kind not in {"intro", "outro"}):
            errors.append(f"scene {scene_id}: Veo is restricted to intro/outro decorative scenes")
        if mode == "veo" and not str(visual.get("veo_prompt", "")).strip():
            errors.append(f"scene {scene_id}: veo mode requires an English veo_prompt")
        if mode == "veo":
            try:
                veo_duration = int(visual.get("veo_duration", 4))
            except (TypeError, ValueError):
                veo_duration = -1
            veo_resolution = str(visual.get("veo_resolution", "720p"))
            if veo_duration not in {4, 6, 8}:
                errors.append(f"scene {scene_id}: veo_duration must be 4, 6, or 8")
            if veo_resolution not in {"720p", "1080p", "4k"}:
                errors.append(f"scene {scene_id}: veo_resolution must be 720p, 1080p, or 4k")
            if veo_resolution in {"1080p", "4k"} and veo_duration != 8:
                errors.append(f"scene {scene_id}: {veo_resolution} Veo output requires an 8-second duration")
        points = visual.get("points")
        if points is not None:
            if not isinstance(points, list) or not all(isinstance(point, str) for point in points):
                errors.append(f"scene {scene_id}: visual.points must be an array of strings")
            elif len(points) > 6:
                errors.append(f"scene {scene_id}: visual.points allows at most 6 entries")
            elif any(len(point.strip()) > 90 for point in points):
                errors.append(f"scene {scene_id}: each visual.points entry must be at most 90 characters")
        stat = visual.get("stat")
        if stat is not None:
            if not isinstance(stat, dict) or not str(stat.get("value", "")).strip():
                errors.append(f"scene {scene_id}: visual.stat must be an object with a non-empty value")
            else:
                if len(str(stat.get("value", "")).strip()) > 16:
                    errors.append(f"scene {scene_id}: visual.stat.value must be at most 16 characters")
                if len(str(stat.get("label", "")).strip()) > 80:
                    errors.append(f"scene {scene_id}: visual.stat.label must be at most 80 characters")
        if visual.get("diagram") is not None:
            _validate_diagram(scene_id, visual.get("diagram"), errors)
        fit = visual.get("fit")
        if fit is not None and (not isinstance(fit, str) or fit not in ALLOWED_VISUAL_FITS):
            errors.append(f"scene {scene_id}: visual.fit must be one of {sorted(ALLOWED_VISUAL_FITS)}")
        broll_paths = visual.get("broll_paths")
        if broll_paths is not None:
            if not isinstance(broll_paths, list) or not all(isinstance(item, str) and item.strip() for item in broll_paths):
                errors.append(f"scene {scene_id}: visual.broll_paths must be an array of non-empty strings")
                broll_paths = None
            elif not broll_paths:
                errors.append(f"scene {scene_id}: visual.broll_paths must not be empty")
                broll_paths = None
            elif len(broll_paths) > MAX_SLIDESHOW_IMAGES:
                errors.append(
                    f"scene {scene_id}: visual.broll_paths allows at most {MAX_SLIDESHOW_IMAGES} images"
                )
            if broll_paths and visual.get("broll_path"):
                errors.append(f"scene {scene_id}: set either visual.broll_path or visual.broll_paths, not both")
        if str(visual.get("mode", "")) == "broll":
            declared = list(broll_paths or ([] if not visual.get("broll_path") else [str(visual["broll_path"])]))
            if not declared:
                errors.append(f"scene {scene_id}: visual.mode=broll requires broll_path or broll_paths")
            else:
                if len(declared) > 1 and any(
                    Path(str(item)).suffix.lower() not in IMAGE_SUFFIXES for item in declared
                ):
                    errors.append(f"scene {scene_id}: visual.broll_paths accepts stills only, not video clips")
                for item in declared if episode_dir else []:
                    try:
                        broll_path = _resolve_local_asset(episode_dir, str(item), f"scene {scene_id} broll_path")
                        if not broll_path.is_file():
                            errors.append(f"scene {scene_id}: broll asset not found: {item}")
                        elif broll_path.suffix.lower() not in BROLL_SUFFIXES:
                            errors.append(f"scene {scene_id}: unsupported broll asset type: {broll_path.suffix}")
                    except ValidationError as exc:
                        errors.append(str(exc))
        if episode_dir and visual.get("image_path"):
            try:
                image_path = _resolve_local_asset(episode_dir, str(visual["image_path"]), f"scene {scene_id} image_path")
                if not image_path.is_file():
                    errors.append(f"scene {scene_id}: image not found: {visual['image_path']}")
            except ValidationError as exc:
                errors.append(str(exc))

    if isinstance(profile, str) and profile in PROFILE_RULES and factual_count > PROFILE_RULES[profile]["max_factual_scenes"]:
        errors.append(
            f"profile {profile} allows at most {PROFILE_RULES[profile]['max_factual_scenes']} factual scenes"
        )
    if isinstance(profile, str) and profile in PROFILE_RULES:
        target_seconds = PROFILE_RULES[profile]["target_seconds"]
        if isinstance(target_seconds, tuple) and len(target_seconds) == 2:
            max_target = float(target_seconds[1])
            if narration_total > max_target:
                errors.append(
                    f"profile {profile} limits estimated total narration to {int(max_target)} seconds"
                )
    if narration_total <= 0:
        warnings.append("episode has no narration; renderer will create a caption-only silent track")

    coverage = episode.get("coverage")
    if not isinstance(coverage, dict):
        errors.append("coverage must be an object")
    else:
        for key in ("scope", "inputs_checked", "limitations"):
            if not isinstance(coverage.get(key), list):
                errors.append(f"coverage.{key} must be an array")

    if errors:
        raise ValidationError("\n".join(f"- {error}" for error in errors))
    return warnings


def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(FONT_BOLD if bold else FONT_REGULAR, max(10, int(size)))


def _rgb(hex_value: str, default: str = "#5DE1FF") -> tuple[int, int, int]:
    value = str(hex_value or default).strip()
    if not re.fullmatch(r"#[0-9a-fA-F]{6}", value):
        value = default
    return tuple(int(value[i : i + 2], 16) for i in (1, 3, 5))


def _cover(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    image = ImageOps.exif_transpose(image).convert("RGB")
    return ImageOps.fit(image, size, method=Image.Resampling.LANCZOS, centering=(0.5, 0.45))


def _wrap_visual(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
    max_lines: int,
) -> list[str]:
    words = re.sub(r"\s+", " ", str(text or "").strip()).split(" ") if str(text or "").strip() else []
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = word if not current else f"{current} {word}"
        if draw.textbbox((0, 0), candidate, font=font)[2] <= max_width:
            current = candidate
            continue
        if current:
            lines.append(current)
        current = word
        if len(lines) >= max_lines:
            break
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(lines) == max_lines and words:
        consumed = " ".join(lines)
        original = " ".join(words)
        if consumed != original:
            while lines[-1] and draw.textbbox((0, 0), lines[-1] + "…", font=font)[2] > max_width:
                lines[-1] = lines[-1][:-1]
            lines[-1] = lines[-1].rstrip() + "…"
    return lines


def _draw_wrapped(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int] | tuple[int, int, int, int],
    max_width: int,
    max_lines: int,
    spacing: int,
) -> int:
    lines = _wrap_visual(draw, text, font, max_width, max_lines)
    x, y = xy
    line_height = int(font.size * 1.22)
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += line_height + spacing
    return y


def _draw_fitted(
    draw: ImageDraw.ImageDraw,
    xy: tuple[int, int],
    text: str,
    *,
    base_size: float,
    min_size: float,
    bold: bool,
    fill: tuple[int, int, int] | tuple[int, int, int, int],
    max_width: int,
    avail_height: int,
    line_cap: int,
    spacing: int,
) -> int:
    """Draw text shrinking the font until the block fits ``avail_height``.

    Text blocks and the fixed-position panels/boxes below them must never
    overlap; when even the minimum size cannot hold the full text, the last
    line is ellipsized by ``_wrap_visual`` within the height budget.
    """
    text = str(text or "").strip()
    if not text or avail_height <= 0:
        return xy[1]
    size = int(base_size)
    floor = max(10, int(min_size))
    while True:
        font = _font(size, bold=bold)
        line_step = int(font.size * 1.22) + spacing
        max_lines = min(line_cap, max(1, avail_height // line_step))
        lines = _wrap_visual(draw, text, font, max_width, max_lines)
        truncated = bool(lines) and lines[-1].endswith("…") and not text.endswith("…")
        if (not truncated and len(lines) * line_step <= avail_height) or size <= floor:
            break
        size = max(floor, size - max(2, int(size * 0.08)))
    x, y = xy
    for line in lines:
        draw.text((x, y), line, font=font, fill=fill)
        y += line_step
    return y


def _gradient_background(width: int, height: int, accent: tuple[int, int, int]) -> Image.Image:
    image = Image.new("RGB", (width, height), (7, 11, 20))
    px = image.load()
    for y in range(height):
        t = y / max(height - 1, 1)
        base = (
            int(7 + 7 * t + accent[0] * 0.035 * (1 - t)),
            int(11 + 5 * t + accent[1] * 0.030 * (1 - t)),
            int(20 + 16 * t + accent[2] * 0.045 * (1 - t)),
        )
        for x in range(width):
            px[x, y] = base
    glow = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow, "RGBA")
    gd.ellipse(
        (int(width * 0.45), int(-height * 0.12), int(width * 1.18), int(height * 0.38)),
        fill=(*accent, 80),
    )
    gd.ellipse(
        (int(-width * 0.4), int(height * 0.62), int(width * 0.48), int(height * 1.18)),
        fill=(127, 92, 255, 55),
    )
    glow = glow.filter(ImageFilter.GaussianBlur(max(20, width // 9)))
    return Image.alpha_composite(image.convert("RGBA"), glow)


def _source_label(source: dict[str, Any]) -> str:
    publisher = str(source.get("publisher", "")).strip()
    if publisher:
        return publisher
    url = str(source.get("url", ""))
    parsed = urlparse(url)
    return parsed.netloc.removeprefix("www.") or str(source.get("title", "Источник"))


def _theme_blob(scene: dict[str, Any]) -> str:
    visual = scene.get("visual") or {}
    parts = [
        str(visual.get("subject", "")),
        str(visual.get("brief", "")),
        str(visual.get("icon_hint", "")),
        str(scene.get("kicker", "")),
        str(scene.get("headline", "")),
        str(scene.get("body", "")),
        str(scene.get("narration", "")),
    ]
    return " ".join(part for part in parts if part).lower()


def _scene_theme(scene: dict[str, Any]) -> dict[str, str]:
    blob = _theme_blob(scene)
    for keywords, label, icon, theme_accent in SCENE_THEME_RULES:
        if any(keyword in blob for keyword in keywords):
            return {"label": label, "icon": icon, "accent": theme_accent}
    visual = scene.get("visual") or {}
    subject = str(visual.get("subject") or visual.get("brief") or scene.get("headline", "")).strip()
    if subject:
        return {"label": subject[:44], "icon": "panel", "accent": str(visual.get("accent", "#5DE1FF"))}
    return {"label": "News / context", "icon": "panel", "accent": str(visual.get("accent", "#5DE1FF"))}


def _draw_theme_icon(
    draw: ImageDraw.ImageDraw,
    box: tuple[int, int, int, int],
    *,
    icon: str,
    accent: tuple[int, int, int],
    scale: float,
) -> None:
    x1, y1, x2, y2 = box
    w = max(1, x2 - x1)
    h = max(1, y2 - y1)
    stroke = max(2, int(4 * scale))
    inset = max(4, int(min(w, h) * 0.08))
    cx = x1 + w // 2
    cy = y1 + h // 2

    def _line(a: tuple[int, int], b: tuple[int, int], *, width: int = stroke) -> None:
        draw.line((a[0], a[1], b[0], b[1]), fill=(*accent, 235), width=width)

    def _ellipse(bounds: tuple[int, int, int, int], *, fill: tuple[int, int, int, int] | None = None) -> None:
        draw.ellipse(bounds, outline=(*accent, 235), fill=fill, width=stroke)

    if icon == "chip":
        inner = (x1 + inset, y1 + inset, x2 - inset, y2 - inset)
        draw.rounded_rectangle(inner, radius=max(8, int(min(w, h) * 0.12)), outline=(*accent, 235), width=stroke)
        pin_len = max(6, int(min(w, h) * 0.12))
        for offset in (-0.34, -0.12, 0.12, 0.34):
            px = int(cx + offset * w * 0.8)
            _line((px, y1), (px, y1 + pin_len), width=max(2, stroke - 1))
            _line((px, y2 - pin_len), (px, y2), width=max(2, stroke - 1))
        for offset in (-0.34, -0.12, 0.12, 0.34):
            py = int(cy + offset * h * 0.8)
            _line((x1, py), (x1 + pin_len, py), width=max(2, stroke - 1))
            _line((x2 - pin_len, py), (x2, py), width=max(2, stroke - 1))
        _ellipse((cx - inset * 2, cy - inset * 2, cx + inset * 2, cy + inset * 2), fill=(*accent, 60))
        return
    if icon == "chart":
        base_y = y2 - inset
        bars = [0.28, 0.52, 0.78]
        bar_w = max(8, int(w * 0.13))
        gap = max(4, int(w * 0.08))
        start_x = cx - (bar_w * 3 + gap * 2) // 2
        for index, frac in enumerate(bars):
            top = int(base_y - frac * (h - inset * 2))
            left = start_x + index * (bar_w + gap)
            draw.rounded_rectangle((left, top, left + bar_w, base_y), radius=max(4, bar_w // 4), fill=(*accent, 200))
        _line((x1 + inset, y1 + inset * 2), (x2 - inset, y1 + inset * 2))
        _line((x1 + inset, base_y), (x2 - inset, base_y), width=max(2, stroke - 1))
        return
    if icon == "scales":
        _line((cx, y1 + inset), (cx, y2 - inset))
        _line((x1 + inset, y1 + inset * 2), (x2 - inset, y1 + inset * 2))
        _line((cx - int(w * 0.22), y1 + int(h * 0.22)), (cx + int(w * 0.22), y1 + int(h * 0.22)))
        _ellipse((cx - inset * 2, y1 + inset, cx + inset * 2, y1 + inset * 4), fill=(*accent, 70))
        left_pan = (cx - int(w * 0.28), y1 + int(h * 0.34), cx - int(w * 0.12), y1 + int(h * 0.48))
        right_pan = (cx + int(w * 0.12), y1 + int(h * 0.34), cx + int(w * 0.28), y1 + int(h * 0.48))
        draw.arc(left_pan, 0, 180, fill=(*accent, 235), width=stroke)
        draw.arc(right_pan, 0, 180, fill=(*accent, 235), width=stroke)
        return
    if icon == "shield":
        pts = [
            (cx, y1 + inset),
            (x2 - inset, y1 + int(h * 0.24)),
            (x2 - int(w * 0.78), y1 + int(h * 0.63)),
            (cx, y2 - inset),
            (x1 + int(w * 0.22), y1 + int(h * 0.63)),
        ]
        draw.polygon(pts, outline=(*accent, 235), fill=(*accent, 50))
        _line((cx, y1 + int(h * 0.22)), (cx, y2 - int(h * 0.16)))
        return
    if icon == "gavel":
        _line((x1 + int(w * 0.30), y1 + int(h * 0.28)), (x2 - int(w * 0.10), y2 - int(h * 0.12)), width=max(3, stroke - 1))
        draw.rounded_rectangle((x1 + int(w * 0.24), y1 + int(h * 0.14), x1 + int(w * 0.54), y1 + int(h * 0.34)), radius=max(4, int(min(w, h) * 0.06)), outline=(*accent, 235), fill=(*accent, 40), width=stroke)
        draw.rounded_rectangle((x1 + int(w * 0.10), y2 - int(h * 0.22), x1 + int(w * 0.88), y2 - int(h * 0.12)), radius=max(4, int(min(w, h) * 0.05)), outline=(*accent, 235), fill=(*accent, 20), width=stroke)
        return
    if icon == "atom":
        _ellipse((cx - int(w * 0.12), cy - int(h * 0.12), cx + int(w * 0.12), cy + int(h * 0.12)), fill=(*accent, 140))
        for angle in (0, 60, 120):
            draw.arc((x1 + inset, y1 + inset, x2 - inset, y2 - inset), angle, angle + 300, fill=(*accent, 225), width=stroke)
        return
    if icon == "rocket":
        draw.polygon(
            [
                (cx, y1 + inset),
                (x2 - inset, cy),
                (cx, y2 - inset),
                (x1 + inset, cy),
            ],
            outline=(*accent, 235),
            fill=(*accent, 45),
        )
        _line((cx, cy), (cx, y2 - inset), width=max(2, stroke - 1))
        draw.arc((x1 + inset, y2 - int(h * 0.30), x2 - inset, y2 - int(h * 0.08)), 200, 340, fill=(*accent, 235), width=stroke)
        return
    if icon == "bolt":
        draw.polygon(
            [
                (cx - int(w * 0.10), y1 + inset),
                (cx + int(w * 0.10), y1 + inset),
                (cx - int(w * 0.04), cy),
                (cx + int(w * 0.18), cy),
                (cx - int(w * 0.08), y2 - inset),
                (cx - int(w * 0.02), cy + int(h * 0.02)),
                (cx - int(w * 0.18), cy + int(h * 0.02)),
            ],
            outline=(*accent, 235),
            fill=(*accent, 80),
        )
        return
    if icon == "plane":
        _line((x1 + inset, cy), (x2 - inset, cy))
        _line((cx, y1 + inset), (cx, y2 - inset))
        _line((cx, cy), (x2 - inset, y1 + int(h * 0.30)))
        _line((cx, cy), (x2 - inset, y2 - int(h * 0.30)))
        return
    # "panel" and default fall back to a dense dashboard glyph.
    _line((x1 + inset, y1 + inset), (x2 - inset, y1 + inset))
    _line((x1 + inset, y2 - inset), (x2 - inset, y2 - inset))
    _line((x1 + inset, y1 + inset), (x1 + inset, y2 - inset))
    _line((x2 - inset, y1 + inset), (x2 - inset, y2 - inset))
    for frac in (0.24, 0.52, 0.78):
        _line((x1 + int(w * 0.18), int(y1 + h * frac)), (x2 - int(w * 0.18), int(y1 + h * frac)), width=max(1, stroke - 2))


MOTION_STYLES = {"aurora", "ocean", "stars", "smoke", "rain", "snow", "sun", "fog"}
MOTION_CACHE_DIR = Path(os.environ.get("BUTLER_NEWS_VIDEO_MOTION_CACHE", DEFAULT_WORKSPACE / "assets" / "motion-cache"))
MOTION_LOOP_SECONDS = 8

DIAGRAM_TYPES = {"bars", "line", "donut", "timeline", "flow", "compare", "weather", "market"}
DIAGRAM_PALETTE = ("#5DE1FF", "#F59E0B", "#34D399", "#F472B6", "#A78BFA", "#94A3B8")


def _diagram_display(value: float, display: Any, unit: str) -> str:
    text = str(display).strip() if display is not None and str(display).strip() else None
    if text is None:
        number = float(value)
        text = f"{int(number):,}".replace(",", " ") if number.is_integer() else f"{number:.1f}"
        text = f"{text}{unit}"
    return text


def _diagram_color(index: int, accent: tuple[int, int, int]) -> tuple[int, int, int]:
    if index == 0:
        return accent
    return _rgb(DIAGRAM_PALETTE[index % len(DIAGRAM_PALETTE)])


def _draw_bars(pd: ImageDraw.ImageDraw, box: tuple[int, int, int, int], diagram: dict[str, Any], accent: tuple[int, int, int], scale: float) -> None:
    x1, y1, x2, y2 = box
    items = list(diagram.get("items") or [])[:5]
    if not items:
        return
    unit = str(diagram.get("unit", "")).strip()
    label_font = _font(23 * scale)
    value_font = _font(26 * scale, bold=True)
    row_h = (y2 - y1) / len(items)
    label_w = int((x2 - x1) * 0.36)
    bar_left = x1 + label_w + int(18 * scale)
    values = [float(item.get("value", 0)) for item in items]
    vmax = max((abs(v) for v in values), default=1.0) or 1.0
    displays = [_diagram_display(v, item.get("display"), unit) for v, item in zip(values, items)]
    value_w = max(pd.textbbox((0, 0), d, font=value_font)[2] for d in displays)
    bar_area = max(x2 - bar_left - value_w - int(14 * scale), int(40 * scale))
    strongest = max(range(len(values)), key=lambda i: abs(values[i]))
    for index, item in enumerate(items):
        cy = y1 + index * row_h + row_h / 2
        lines = _wrap_visual(pd, str(item.get("label", "")), label_font, label_w, 2)
        line_h = label_font.size * 1.2
        ly = cy - line_h * len(lines) / 2
        for line in lines:
            pd.text((x1, int(ly)), line, font=label_font, fill=(214, 224, 238, 240))
            ly += line_h
        width = max(int(abs(values[index]) / vmax * bar_area), int(10 * scale))
        bar_h = min(int(30 * scale), int(row_h * 0.48))
        alpha = 235 if index == strongest else 150
        pd.rounded_rectangle(
            (bar_left, int(cy - bar_h / 2), bar_left + width, int(cy + bar_h / 2)),
            radius=bar_h // 2,
            fill=(*accent, alpha),
        )
        pd.text((bar_left + width + int(12 * scale), int(cy - value_font.size * 0.62)), displays[index], font=value_font, fill=(240, 245, 251, 250))


def _draw_line(pd: ImageDraw.ImageDraw, box: tuple[int, int, int, int], diagram: dict[str, Any], accent: tuple[int, int, int], scale: float) -> None:
    x1, y1, x2, y2 = box
    points = list(diagram.get("points") or [])[:8]
    if len(points) < 2:
        return
    unit = str(diagram.get("unit", "")).strip()
    values = [float(point.get("value", 0)) for point in points]
    label_font = _font(20 * scale)
    value_font = _font(24 * scale, bold=True)
    plot_top = y1 + int(34 * scale)
    plot_bottom = y2 - int(38 * scale)
    vmin, vmax = min(values), max(values)
    span = (vmax - vmin) or abs(vmax) or 1.0
    pad = span * 0.15
    lo, hi = vmin - pad, vmax + pad
    xs = [x1 + int((x2 - x1 - int(20 * scale)) * i / (len(points) - 1)) + int(10 * scale) for i in range(len(points))]
    ys = [int(plot_bottom - (v - lo) / (hi - lo) * (plot_bottom - plot_top)) for v in values]
    for frac in (0.0, 0.5, 1.0):
        gy = int(plot_top + (plot_bottom - plot_top) * frac)
        pd.line((x1, gy, x2, gy), fill=(255, 255, 255, 26), width=max(1, int(scale)))
    pd.line(list(zip(xs, ys)), fill=(*accent, 235), width=max(3, int(5 * scale)), joint="curve")
    radius = max(4, int(7 * scale))
    for index, (px, py) in enumerate(zip(xs, ys)):
        pd.ellipse((px - radius, py - radius, px + radius, py + radius), fill=(*accent, 255))
        pd.text((px - int(10 * scale), plot_bottom + int(10 * scale)), str(points[index].get("label", ""))[:8], font=label_font, fill=(178, 191, 209, 230))
    for index in (0, len(points) - 1):
        text = _diagram_display(values[index], points[index].get("display"), unit)
        tw = pd.textbbox((0, 0), text, font=value_font)[2]
        tx = min(max(xs[index] - tw // 2, x1), x2 - tw)
        pd.text((tx, ys[index] - int(34 * scale)), text, font=value_font, fill=(240, 245, 251, 250))
    delta = values[-1] - values[0]
    if delta:
        arrow = "↓" if delta < 0 else "↑"
        badge = f"{arrow} {_diagram_display(abs(delta), None, unit)}"
        bw = pd.textbbox((0, 0), badge, font=value_font)[2]
        pd.text((x2 - bw, y1), badge, font=value_font, fill=(*accent, 255))


def _draw_donut(pd: ImageDraw.ImageDraw, box: tuple[int, int, int, int], diagram: dict[str, Any], accent: tuple[int, int, int], scale: float) -> None:
    x1, y1, x2, y2 = box
    items = list(diagram.get("items") or [])[:4]
    values = [max(float(item.get("value", 0)), 0.0) for item in items]
    total = sum(values)
    if total <= 0:
        return
    size = min(y2 - y1, int((x2 - x1) * 0.42))
    ring = (x1, y1 + (y2 - y1 - size) // 2, x1 + size, y1 + (y2 - y1 + size) // 2)
    width = max(10, int(size * 0.17))
    start = -90.0
    for index, value in enumerate(values):
        extent = value / total * 360.0
        color = _diagram_color(index, accent)
        pd.arc(ring, start, start + extent - 1.5, fill=(*color, 240), width=width)
        start += extent
    legend_font = _font(23 * scale)
    pct_font = _font(23 * scale, bold=True)
    legend_x = x1 + size + int(34 * scale)
    row_h = min(int(46 * scale), (y2 - y1) // max(len(items), 1))
    legend_y = y1 + ((y2 - y1) - row_h * len(items)) // 2
    for index, item in enumerate(items):
        color = _diagram_color(index, accent)
        dot = int(14 * scale)
        cy = legend_y + index * row_h + row_h // 2
        pd.ellipse((legend_x, cy - dot // 2, legend_x + dot, cy + dot // 2), fill=(*color, 255))
        pct = f"{values[index] / total * 100:.0f}%"
        pd.text((legend_x + dot + int(12 * scale), cy - int(legend_font.size * 0.62)), f"{pct} · {str(item.get('label', ''))[:26]}", font=pct_font if index == 0 else legend_font, fill=(230, 237, 248, 245))


def _draw_timeline(pd: ImageDraw.ImageDraw, box: tuple[int, int, int, int], diagram: dict[str, Any], accent: tuple[int, int, int], scale: float) -> None:
    x1, y1, x2, y2 = box
    steps = list(diagram.get("steps") or [])[:5]
    if not steps:
        return
    label_font = _font(26 * scale, bold=True)
    detail_font = _font(21 * scale)
    line_x = x1 + int(12 * scale)
    row_h = (y2 - y1) / len(steps)
    pd.line((line_x, y1 + int(row_h * 0.3), line_x, y2 - int(row_h * 0.55)), fill=(*accent, 130), width=max(2, int(3 * scale)))
    for index, step in enumerate(steps):
        cy = int(y1 + index * row_h + row_h * 0.3)
        radius = int(10 * scale)
        pd.ellipse((line_x - radius, cy - radius, line_x + radius, cy + radius), fill=(*accent, 255) if index == 0 else (9, 16, 28, 255), outline=(*accent, 255), width=max(2, int(2 * scale)))
        text_x = line_x + radius + int(18 * scale)
        pd.text((text_x, cy - int(label_font.size * 0.62)), str(step.get("label", ""))[:44], font=label_font, fill=(240, 245, 251, 250))
        detail = str(step.get("detail", "")).strip()
        if detail:
            for line in _wrap_visual(pd, detail, detail_font, x2 - text_x, 1):
                pd.text((text_x, cy + int(label_font.size * 0.55)), line, font=detail_font, fill=(186, 198, 215, 235))


def _draw_flow(pd: ImageDraw.ImageDraw, box: tuple[int, int, int, int], diagram: dict[str, Any], accent: tuple[int, int, int], scale: float) -> None:
    x1, y1, x2, y2 = box
    steps = list(diagram.get("steps") or [])[:4]
    if not steps:
        return
    label_font = _font(25 * scale, bold=True)
    detail_font = _font(20 * scale)
    gap = int(24 * scale)
    box_h = int(((y2 - y1) - gap * (len(steps) - 1)) / len(steps))
    cx = (x1 + x2) // 2
    for index, step in enumerate(steps):
        top = y1 + index * (box_h + gap)
        pd.rounded_rectangle((x1, top, x2, top + box_h), radius=int(16 * scale), fill=(*accent, 34), outline=(*accent, 200), width=max(2, int(2 * scale)))
        label = str(step.get("label", ""))[:44]
        detail = str(step.get("detail", "")).strip()
        lw = pd.textbbox((0, 0), label, font=label_font)[2]
        if detail:
            pd.text((cx - lw // 2, top + int(box_h * 0.16)), label, font=label_font, fill=(240, 245, 251, 250))
            lines = _wrap_visual(pd, detail, detail_font, x2 - x1 - int(40 * scale), 1)
            if lines:
                dw = pd.textbbox((0, 0), lines[0], font=detail_font)[2]
                pd.text((cx - dw // 2, top + int(box_h * 0.55)), lines[0], font=detail_font, fill=(186, 198, 215, 235))
        else:
            pd.text((cx - lw // 2, top + (box_h - label_font.size) // 2), label, font=label_font, fill=(240, 245, 251, 250))
        if index < len(steps) - 1:
            ay = top + box_h + gap // 2
            arrow = max(6, int(9 * scale))
            pd.polygon([(cx - arrow, ay - arrow // 2), (cx + arrow, ay - arrow // 2), (cx, ay + arrow)], fill=(*accent, 220))


def _draw_compare(pd: ImageDraw.ImageDraw, box: tuple[int, int, int, int], diagram: dict[str, Any], accent: tuple[int, int, int], scale: float) -> None:
    x1, y1, x2, y2 = box
    left = diagram.get("left") or {}
    right = diagram.get("right") or {}
    mid = (x1 + x2) // 2
    pd.line((mid, y1 + int(6 * scale), mid, y2 - int(6 * scale)), fill=(255, 255, 255, 40), width=max(1, int(2 * scale)))
    title_font = _font(26 * scale, bold=True)
    item_font = _font(22 * scale)
    for side, column in ((left, (x1, mid - int(20 * scale))), (right, (mid + int(20 * scale), x2))):
        cx1, cx2 = column
        color = accent if side is left else _diagram_color(1, accent)
        pd.text((cx1, y1), str(side.get("title", ""))[:32].upper(), font=title_font, fill=(*color, 255))
        item_y = y1 + int(44 * scale)
        for item in list(side.get("items") or [])[:4]:
            dot = int(9 * scale)
            pd.ellipse((cx1, item_y + int(item_font.size * 0.32), cx1 + dot, item_y + int(item_font.size * 0.32) + dot), fill=(*color, 235))
            item_y = _draw_wrapped(pd, (cx1 + dot + int(12 * scale), item_y), str(item), item_font, (228, 235, 246, 245), cx2 - cx1 - dot - int(12 * scale), 2, int(2 * scale))
            item_y += int(6 * scale)


MARKET_UP = (48, 209, 88)
MARKET_DOWN = (255, 91, 82)


def _draw_market(pd: ImageDraw.ImageDraw, box: tuple[int, int, int, int], diagram: dict[str, Any], accent: tuple[int, int, int], scale: float) -> None:
    """Ticker-style market panel: label, price, colored change chip, sparkline."""
    x1, y1, x2, y2 = box
    items = [row for row in (diagram.get("items") or []) if isinstance(row, dict)][:6]
    if not items:
        return
    row_h = min(int(84 * scale), (y2 - y1) // len(items))
    label_font = _font(34 * scale, bold=True)
    value_font = _font(34 * scale, bold=True)
    chip_font = _font(26 * scale, bold=True)
    for index, row in enumerate(items):
        top = y1 + index * row_h
        mid = top + row_h / 2
        if index:
            pd.line((x1, top, x2, top), fill=(255, 255, 255, 22), width=max(1, int(scale)))
        label = str(row.get("label", ""))[:14]
        pd.text((x1, mid - label_font.size * 0.62), label, font=label_font, fill=(240, 244, 250, 255))
        # sparkline in the middle third when provided
        spark = row.get("spark")
        if isinstance(spark, list) and len(spark) >= 2:
            sx1 = x1 + (x2 - x1) * 0.34
            sx2 = x1 + (x2 - x1) * 0.56
            lo, hi = min(spark), max(spark)
            span = (hi - lo) or 1.0
            pts = []
            for i, v in enumerate(spark):
                px = sx1 + (sx2 - sx1) * i / (len(spark) - 1)
                py = mid + row_h * 0.26 - (float(v) - lo) / span * row_h * 0.52
                pts.append((px, py))
            up = float(spark[-1]) >= float(spark[0])
            pd.line(pts, fill=(*(MARKET_UP if up else MARKET_DOWN), 235), width=max(2, int(3 * scale)), joint="curve")
        # price right-of-center
        value = str(row.get("value", ""))[:12]
        vw = pd.textbbox((0, 0), value, font=value_font)[2]
        value_right = x1 + (x2 - x1) * 0.78
        pd.text((value_right - vw, mid - value_font.size * 0.62), value, font=value_font, fill=(226, 233, 245, 250))
        # change chip
        change = row.get("change_pct")
        if change is not None:
            up = float(change) >= 0
            color = MARKET_UP if up else MARKET_DOWN
            text = f"{abs(float(change)):.2f}".rstrip("0").rstrip(".") + "%"
            tw = pd.textbbox((0, 0), text, font=chip_font)[2]
            tri = int(12 * scale)
            chip_w = tw + tri + int(40 * scale)
            cx2 = x2
            cx1 = cx2 - chip_w
            cy1 = mid - int(24 * scale)
            cy2 = mid + int(24 * scale)
            pd.rounded_rectangle((cx1, cy1, cx2, cy2), radius=int(14 * scale), fill=(*color, 42), outline=(*color, 160), width=max(1, int(scale)))
            tx = cx1 + int(14 * scale)
            ty = mid
            if up:
                pd.polygon([(tx, ty + tri / 2), (tx + tri, ty + tri / 2), (tx + tri / 2, ty - tri / 2)], fill=(*color, 255))
            else:
                pd.polygon([(tx, ty - tri / 2), (tx + tri, ty - tri / 2), (tx + tri / 2, ty + tri / 2)], fill=(*color, 255))
            pd.text((tx + tri + int(8 * scale), mid - chip_font.size * 0.62), text, font=chip_font, fill=(*color, 255))


def _draw_weather(pd: ImageDraw.ImageDraw, box: tuple[int, int, int, int], diagram: dict[str, Any], accent: tuple[int, int, int], scale: float) -> None:
    """Dedicated weather panel: current conditions with humidity/pressure/wind
    chips on top, then per-day min-max temperature range bars with rain."""
    x1, y1, x2, y2 = box
    width = x2 - x1
    current = diagram.get("current") if isinstance(diagram.get("current"), dict) else {}
    days = [d for d in (diagram.get("days") or []) if isinstance(d, dict)][:4]
    cursor = y1
    if current:
        temp = str(current.get("temperature", "")).strip()
        cond = str(current.get("condition", "")).strip()
        headline = " · ".join(part for part in (f"{temp}°" if temp else "", cond) if part)
        if headline:
            font = _font(56 * scale, bold=True)
            pd.text((x1, cursor), headline, font=font, fill=(*accent, 255))
            cursor += int(font.size * 1.25)
        chips = []
        if current.get("humidity") not in (None, ""):
            chips.append(f"влажность {current['humidity']}%")
        if current.get("pressure") not in (None, ""):
            chips.append(f"давление {current['pressure']} гПа")
        if current.get("wind") not in (None, ""):
            chips.append(f"ветер {current['wind']} км/ч")
        if chips:
            chip_font = _font(26 * scale)
            chip_x = x1
            for chip in chips:
                text_w = pd.textbbox((0, 0), chip, font=chip_font)[2]
                chip_w = text_w + int(30 * scale)
                if chip_x + chip_w > x2:
                    break
                pd.rounded_rectangle(
                    (chip_x, cursor, chip_x + chip_w, cursor + int(44 * scale)),
                    radius=int(22 * scale),
                    fill=(255, 255, 255, 26),
                    outline=(*accent, 120),
                    width=max(1, int(scale)),
                )
                pd.text((chip_x + int(15 * scale), cursor + int(8 * scale)), chip, font=chip_font, fill=(226, 233, 245, 245))
                chip_x += chip_w + int(12 * scale)
            cursor += int(64 * scale)
    if not days:
        return
    all_min = min(float(d.get("min", 0)) for d in days)
    all_max = max(float(d.get("max", 0)) for d in days)
    span = max(all_max - all_min, 1.0)
    col_w = width / len(days)
    bar_top = cursor + int(38 * scale)
    # A short box must still produce a valid (non-inverted) bar rectangle.
    bar_bottom = max(y2 - int(112 * scale), bar_top + int(40 * scale))
    bar_height = max(bar_bottom - bar_top, int(40 * scale))
    label_font = _font(26 * scale, bold=True)
    value_font = _font(28 * scale, bold=True)
    small_font = _font(22 * scale)
    for index, day in enumerate(days):
        cx = x1 + col_w * index + col_w / 2
        d_min, d_max = float(day.get("min", 0)), float(day.get("max", 0))
        top = bar_top + (all_max - d_max) / span * bar_height
        bottom = bar_top + (all_max - d_min) / span * bar_height
        bottom = max(bottom, top + int(26 * scale))
        bar_w = max(int(18 * scale), int(col_w * 0.16))
        pd.rounded_rectangle(
            (cx - bar_w / 2, bar_top, cx + bar_w / 2, bar_bottom),
            radius=int(bar_w / 2),
            fill=(255, 255, 255, 18),
        )
        pd.rounded_rectangle(
            (cx - bar_w / 2, top, cx + bar_w / 2, bottom),
            radius=int(bar_w / 2),
            fill=(*accent, 235),
        )
        max_text = f"{round(d_max)}°"
        min_text = f"{round(d_min)}°"
        pd.text((cx - pd.textbbox((0, 0), max_text, font=value_font)[2] / 2, top - int(36 * scale)), max_text, font=value_font, fill=(247, 249, 253, 255))
        pd.text((cx - pd.textbbox((0, 0), min_text, font=small_font)[2] / 2, bar_bottom + int(8 * scale)), min_text, font=small_font, fill=(190, 202, 220, 235))
        label = str(day.get("label", ""))[:12]
        pd.text((cx - pd.textbbox((0, 0), label, font=label_font)[2] / 2, y2 - int(62 * scale)), label, font=label_font, fill=(*accent, 255))
        extras = []
        rain = day.get("precipitation_mm")
        if rain not in (None, "") and float(rain) > 0:
            extras.append(f"{rain} мм")
        if day.get("humidity") not in (None, ""):
            extras.append(f"{day['humidity']}%")
        if extras:
            extra_text = " · ".join(extras)
            pd.text((cx - pd.textbbox((0, 0), extra_text, font=small_font)[2] / 2, y2 - int(28 * scale)), extra_text, font=small_font, fill=(170, 184, 205, 220))


# Vertical room each diagram type needs at scale 1.0. A shorter box (the left
# column of a landscape card) shrinks the whole drawing instead of overflowing
# or producing an inverted rectangle.
DIAGRAM_MIN_BOX_HEIGHT: dict[str, int] = {
    "weather": 400,
    "market": 300,
    "bars": 240,
    "line": 240,
    "donut": 240,
    "timeline": 220,
    "flow": 200,
    "compare": 220,
}


def _draw_diagram(pd: ImageDraw.ImageDraw, box: tuple[int, int, int, int], diagram: dict[str, Any], accent: tuple[int, int, int], scale: float) -> None:
    kind = str(diagram.get("type", ""))
    needed = DIAGRAM_MIN_BOX_HEIGHT.get(kind, 240) * scale
    available = box[3] - box[1]
    if needed > 0 and available < needed:
        # Never below 55%: past that the labels stop being readable on a TV.
        scale = max(scale * 0.55, scale * available / needed)
    renderer = {
        "bars": _draw_bars,
        "line": _draw_line,
        "donut": _draw_donut,
        "timeline": _draw_timeline,
        "flow": _draw_flow,
        "compare": _draw_compare,
        "weather": _draw_weather,
        "market": _draw_market,
    }.get(kind)
    if renderer:
        renderer(pd, box, diagram, accent, scale)


def _motion_style(scene: dict[str, Any]) -> str:
    """Pick a living-background style from the scene's theme. Deterministic."""
    kind = str(scene.get("kind", ""))
    if kind == "outro":
        return "ocean"
    blob = _theme_blob(scene)
    icon = _scene_theme(scene)["icon"]
    if icon in {"atom", "rocket"} or any(word in blob for word in ("космос", "space", "planet", "наук", "science", "research", "исследован")):
        return "stars"
    if icon == "shield" or any(word in blob for word in ("война", "war", "удар", "strike", "погиб", "фронт", "оборон", "military")):
        return "smoke"
    if any(word in blob for word in ("погод", "weather", "dwd", "прогноз", "forecast")):
        if any(word in blob for word in ("дожд", "ливн", "rain", "гроза", "storm", "shower")):
            return "rain"
        if any(word in blob for word in ("снег", "snow", "мороз", "гололед", "гололёд", "ice", "frost")):
            return "snow"
        if any(word in blob for word in ("туман", "fog", "пасмур", "cloud", "облач")):
            return "fog"
        if any(word in blob for word in ("солн", "sun", "жара", "heat", "ясно", "clear")):
            return "sun"
        return "ocean"
    if kind == "context":
        return "ocean"
    return "aurora"


def _motion_keyframes(style: str, width: int, height: int, rng: random.Random) -> list[Image.Image]:
    """Seeded keyframes; blob positions close their cycle so the loop is seamless."""
    frame_count = 6
    palettes = {
        "aurora": [(93, 225, 255), (127, 92, 255), (52, 211, 153), (96, 165, 250)],
        "ocean": [(37, 99, 160), (45, 130, 180), (23, 60, 110), (93, 225, 255)],
        "smoke": [(70, 78, 92), (52, 58, 70), (92, 100, 114), (44, 50, 62)],
        "stars": [(235, 240, 250), (93, 225, 255), (200, 210, 230), (167, 139, 250)],
        "rain": [(120, 170, 220), (93, 190, 255), (150, 195, 235)],
        "snow": [(240, 245, 252), (215, 228, 245), (200, 215, 235)],
        "sun": [(255, 200, 90), (255, 170, 60), (255, 226, 150), (250, 140, 66)],
        "fog": [(150, 158, 172), (120, 128, 142), (176, 184, 198)],
    }
    colors = palettes.get(style, palettes["aurora"])
    bases = {"smoke": (10, 11, 14), "rain": (8, 14, 24), "snow": (12, 16, 26), "sun": (18, 12, 6), "fog": (16, 18, 22)}
    base = bases.get(style, (5, 8, 16))
    blob_count = {"aurora": 4, "ocean": 5, "smoke": 3, "stars": 26, "rain": 42, "snow": 30, "sun": 4, "fog": 4}[style]
    blobs = []
    for index in range(blob_count):
        if style == "stars":
            size = rng.randint(max(2, width // 90), max(4, width // 28))
        elif style == "rain":
            size = rng.randint(max(2, width // 160), max(4, width // 90))
        elif style == "snow":
            size = rng.randint(max(3, width // 110), max(6, width // 60))
        elif style == "ocean":
            size = rng.randint(width // 2, width)
        else:
            size = rng.randint(width // 3, int(width * 0.8))
        blobs.append(
            {
                "color": colors[index % len(colors)],
                "size": size,
                "cx": rng.uniform(0.05, 0.95),
                "cy": rng.uniform(0.35, 0.95) if style == "ocean" else rng.uniform(0.05, 0.95),
                "orbit": rng.uniform(0.04, 0.16) * (0.5 if style == "stars" else 2.2 if style in ("sun", "fog") else 1.0),
                "phase": rng.uniform(0, 2 * math.pi),
                "alpha": rng.randint(150, 230) if style in ("stars", "rain", "snow") else rng.randint(80, 130),
                "fall_speed": rng.uniform(0.9, 1.1),
            }
        )
    frames: list[Image.Image] = []
    for frame_index in range(frame_count):
        angle_step = 2 * math.pi * frame_index / frame_count
        layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        ld = ImageDraw.Draw(layer, "RGBA")
        for blob in blobs:
            angle = blob["phase"] + angle_step
            if style in ("rain", "snow"):
                # A whole number of falls per cycle keeps the loop seamless.
                falls = 1 if style == "snow" else 2
                progress = (blob["cy"] + falls * frame_index / frame_count) % 1.0
                cx = blob["cx"] * width + math.sin(angle) * (width * (0.01 if style == "rain" else 0.03))
                cy = progress * (height + blob["size"] * 4) - blob["size"] * 2
                half_w = blob["size"] / 2
                half_h = half_w * (6.0 if style == "rain" else 1.0)
            else:
                cx = blob["cx"] * width + math.cos(angle) * blob["orbit"] * width
                cy = blob["cy"] * height + math.sin(angle) * blob["orbit"] * height * (0.35 if style == "ocean" else 1.0)
                half_w = blob["size"] / 2
                half_h = half_w * (0.28 if style == "ocean" else 1.0)
            ld.ellipse(
                (cx - half_w, cy - half_h, cx + half_w, cy + half_h),
                fill=(*blob["color"], blob["alpha"]),
            )
        blur = max(2, width // (60 if style == "stars" else 110 if style == "rain" else 70 if style == "snow" else 7))
        layer = layer.filter(ImageFilter.GaussianBlur(blur))
        frame = Image.new("RGB", (width, height), base)
        frame = Image.alpha_composite(frame.convert("RGBA"), layer).convert("RGB")
        frames.append(frame)
    return frames


def _ensure_motion_loop(style: str, *, width: int, height: int, fps: int) -> Path:
    """Build (or reuse) a cached seamless background loop for one style/size."""
    if style not in MOTION_STYLES:
        raise RenderError(f"unknown motion style: {style}")
    MOTION_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = MOTION_CACHE_DIR / f"{style}-{width}x{height}-{fps}.mp4"
    if out_path.is_file() and _duration(out_path) >= MOTION_LOOP_SECONDS - 0.5:
        return out_path
    rng = random.Random(f"butler-news-video/{style}/{width}x{height}")
    gen_w = max(180, (width // 2) // 2 * 2)
    gen_h = max(320, (height // 2) // 2 * 2)
    frames = _motion_keyframes(style, gen_w, gen_h, rng)
    with tempfile.TemporaryDirectory(prefix="butler-news-video-motion-") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        # The first frame is appended twice: once to close the loop and once as
        # tail padding — minterpolate stops interpolating at the last input PTS.
        for index, frame in enumerate(frames + [frames[0], frames[0]]):
            frame.save(temp_dir / f"frame-{index:02d}.png", format="PNG")
        temp_out = temp_dir / "loop.mp4"
        input_fps = len(frames) / float(MOTION_LOOP_SECONDS)
        _run(
            [
                FFMPEG,
                "-y",
                "-framerate",
                f"{input_fps:.6f}",
                "-i",
                str(temp_dir / "frame-%02d.png"),
                "-vf",
                f"minterpolate=fps={fps}:mi_mode=blend,scale={width}:{height},format=yuv420p",
                "-t",
                str(MOTION_LOOP_SECONDS),
                "-c:v",
                "libx264",
                "-preset",
                "veryfast",
                "-crf",
                "22",
                "-pix_fmt",
                "yuv420p",
                str(temp_out),
            ],
            timeout=300,
        )
        if _duration(temp_out) < MOTION_LOOP_SECONDS - 0.5:
            raise RenderError(f"motion loop for {style} came out too short")
        os.replace(temp_out, out_path)
    return out_path


@dataclasses.dataclass(frozen=True)
class CardLayout:
    """Where every block of a scene card lives.

    Portrait keeps the historical single-column stack. Landscape (Boris
    2026-08-01, Bamberg on a TV) splits the frame: text and the data panel on
    the left, a media window on the right, captions across the bottom.
    """

    width: int
    height: int
    scale: float
    margin: int
    text_left: int
    text_right: int
    text_top: int
    stage: tuple[int, int, int, int]
    side_panel: tuple[int, int, int, int]
    chips_y: int
    caption_box: tuple[int, int, int, int]
    footer_y: int

    @property
    def landscape(self) -> bool:
        return self.width > self.height


def _even(value: float) -> int:
    """Round down to an even pixel — the media window doubles as an encoder
    frame size, and libx264 rejects odd dimensions."""
    return int(value) // 2 * 2


def _card_layout(width: int, height: int) -> CardLayout:
    if width > height:
        # Height is the constraining axis on a 16:9 frame, so type scales with it.
        scale = height / 1080.0
        margin = int(height * 0.056)
        text_right = int(width * 0.455)
        return CardLayout(
            width=width,
            height=height,
            scale=scale,
            margin=margin,
            text_left=margin,
            text_right=text_right,
            text_top=margin + int(104 * scale),
            stage=(_even(width * 0.485), _even(height * 0.175), _even(width - margin), _even(height * 0.68)),
            side_panel=(margin, int(height * 0.425), text_right, int(height * 0.68)),
            chips_y=int(height * 0.686),
            caption_box=(margin, int(height * 0.73), width - margin, height - int(50 * scale)),
            footer_y=height - int(42 * scale),
        )
    scale = width / 1080.0
    margin = int(width * 0.075)
    return CardLayout(
        width=width,
        height=height,
        scale=scale,
        margin=margin,
        text_left=margin,
        text_right=width - margin,
        text_top=margin + int(112 * scale),
        stage=(margin, int(height * 0.36), width - margin, int(height * 0.58)),
        side_panel=(margin, int(height * 0.36), width - margin, int(height * 0.58)),
        chips_y=int(height * 0.615),
        caption_box=(margin, int(height * 0.69), width - margin, height - margin - int(70 * scale)),
        footer_y=height - margin - int(34 * scale),
    )


def _caption_wave_geometry(width: int, height: int) -> tuple[int, int, int, int]:
    layout = _card_layout(width, height)
    scale = layout.scale
    box_left, box_top, box_right, box_bottom = layout.caption_box
    if layout.landscape:
        # The band is wide but shallow: the wave shares the label row with
        # "ГОЛОС" so the subtitle keeps the full height below it.
        wave_h = max(24, int(36 * scale)) // 2 * 2
        wave_w = max(120, int((box_right - box_left) * 0.30)) // 2 * 2
        wave_x = box_right - int(30 * scale) - wave_w
        wave_y = box_top + int(16 * scale)
        return wave_x, wave_y, wave_w, wave_h
    wave_h = max(24, int(44 * scale)) // 2 * 2
    wave_w = max(120, width - 2 * layout.margin - int(60 * scale)) // 2 * 2
    wave_x = layout.margin + int(30 * scale)
    wave_y = box_bottom - wave_h - int(14 * scale)
    return wave_x, wave_y, wave_w, wave_h


def _render_motion_scene(
    loop: Path,
    card: Path,
    audio: Path,
    out_path: Path,
    *,
    duration: float,
    width: int,
    height: int,
    fps: int,
    accent_hex: str,
) -> None:
    """Living background + static card overlay + voice waveform in the caption box."""
    fade_out = max(0.0, duration - 0.25)
    wave_x, wave_y, wave_w, wave_h = _caption_wave_geometry(width, height)
    accent = "0x%02X%02X%02X" % _rgb(accent_hex)
    filter_complex = (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},fps={fps},"
        f"drawbox=x=0:y=0:w={width}:h={height}:color=black@0.20:t=fill[bg];"
        f"[1:v]scale={width}:{height},format=rgba,fade=t=in:st=0:d=0.35:alpha=1[card];"
        f"[bg][card]overlay=0:0[base];"
        f"[2:a]pan=mono|c0=0.5*c0+0.5*c1,"
        # draw=full: the default additive draw accumulates overlapping samples
        # in YUV and shifts the accent hue toward dirty yellow.
        f"showwaves=s={wave_w}x{wave_h}:mode=cline:rate={fps}:draw=full:colors={accent},"
        f"format=rgba,colorkey=0x000000:0.08:0.0[wave];"
        f"[base][wave]overlay={wave_x}:{wave_y}:format=auto,"
        f"fade=t=in:st=0:d=0.20,fade=t=out:st={fade_out:.3f}:d=0.25,format=yuv420p[v]"
    )
    _run(
        [
            FFMPEG,
            "-y",
            "-stream_loop",
            "-1",
            "-i",
            str(loop),
            "-loop",
            "1",
            "-framerate",
            str(fps),
            "-i",
            str(card),
            "-i",
            str(audio),
            "-t",
            f"{duration:.3f}",
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-map",
            "2:a:0",
            "-af",
            "loudnorm=I=-16:TP=-1:LRA=11",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(out_path),
        ],
        timeout=max(240, int(duration * 10)),
    )


def _panel_has_diagram(visual: dict[str, Any]) -> bool:
    diagram = visual.get("diagram")
    return isinstance(diagram, dict) and str(diagram.get("type", "")) in DIAGRAM_TYPES


def _draw_card_chrome(
    draw: ImageDraw.ImageDraw,
    *,
    episode: dict[str, Any],
    scene: dict[str, Any],
    layout: CardLayout,
    accent: tuple[int, int, int],
) -> None:
    """Badge, status, progress bar and position counter — identical in both
    orientations, only the geometry comes from the layout."""
    scale, margin, width = layout.scale, layout.margin, layout.width
    # The badge text is configurable per episode (episode.branding.badge) so
    # other publications keep their own identity.
    badge = str((episode.get("branding") or {}).get("badge") or "AI BUTLER")[:20]
    badge_font = _font(27 * scale, bold=True)
    badge_w = draw.textbbox((0, 0), badge, font=badge_font)[2] + int(44 * scale)
    draw.rounded_rectangle(
        (margin, margin, margin + badge_w, margin + int(54 * scale)),
        radius=int(27 * scale),
        fill=(*accent, 230),
    )
    draw.text((margin + int(22 * scale), margin + int(11 * scale)), badge, font=badge_font, fill=(2, 12, 20, 255))
    status = str(scene.get("status", "not-applicable"))
    status_labels = {
        "confirmed": "ПОДТВЕРЖДЕНО",
        "developing": "РАЗВИВАЕТСЯ",
        "context": "КОНТЕКСТ",
        "not-applicable": "ОБЗОР",
    }
    status_text = status_labels.get(status, status.upper())
    status_font = _font(24 * scale, bold=True)
    status_w = draw.textbbox((0, 0), status_text, font=status_font)[2]
    draw.text((width - margin - status_w, margin + int(13 * scale)), status_text, font=status_font, fill=(225, 235, 248, 235))

    # Progress: a viewer should always know where they are in the episode.
    scene_list = [item for item in episode.get("scenes") or [] if isinstance(item, dict)]
    total_scenes = len(scene_list)
    position = next(
        (index + 1 for index, item in enumerate(scene_list) if item.get("id") == scene.get("id")),
        0,
    )
    if total_scenes > 1 and position:
        bar_h = max(4, int(8 * scale))
        draw.rectangle((0, 0, width, bar_h), fill=(255, 255, 255, 30))
        draw.rectangle((0, 0, int(width * position / total_scenes), bar_h), fill=(*accent, 235))
        progress_text = f"{position}/{total_scenes}"
        progress_font = _font(24 * scale, bold=True)
        progress_w = draw.textbbox((0, 0), progress_text, font=progress_font)[2]
        draw.text(
            (width - margin - progress_w, margin + int(50 * scale)),
            progress_text,
            font=progress_font,
            fill=(174, 188, 207, 235),
        )


def _draw_insight_panel(
    pd: ImageDraw.ImageDraw,
    panel_box: tuple[int, int, int, int],
    *,
    scene: dict[str, Any],
    visual: dict[str, Any],
    scale: float,
) -> None:
    """The explanatory core for a visual learner. Priority: a real diagram beats
    a big concrete number (visual.stat), which beats a bullet list
    (visual.points), which beats the abstract brief+icon fallback."""
    theme = _scene_theme(scene)
    theme_accent = _rgb(theme["accent"], default="#5DE1FF")
    stat = visual.get("stat") if isinstance(visual.get("stat"), dict) else {}
    stat_value = str(stat.get("value", "")).strip()
    raw_points = visual.get("points") if isinstance(visual.get("points"), list) else []
    points = [str(point).strip() for point in raw_points if str(point).strip()][:4]
    diagram = visual.get("diagram") if isinstance(visual.get("diagram"), dict) else {}
    has_diagram = _panel_has_diagram(visual)
    pd.rounded_rectangle(panel_box, radius=int(38 * scale), fill=(9, 16, 28, 205), outline=(*theme_accent, 130), width=max(2, int(2 * scale)))
    pad = int(28 * scale)
    inner_left = panel_box[0] + pad
    inner_right = panel_box[2] - pad
    label_font = _font(26 * scale, bold=True)
    pd.text((inner_left, panel_box[1] + int(22 * scale)), theme["label"].upper(), font=label_font, fill=(*theme_accent, 255))
    content_top = panel_box[1] + int(68 * scale)
    small_icon_side = int((panel_box[3] - panel_box[1]) * 0.30)
    if has_diagram:
        # A real diagram is the strongest explanation; it replaces icon,
        # stat and points entirely.
        _draw_diagram(
            pd,
            (inner_left, content_top, inner_right, panel_box[3] - pad),
            diagram,
            theme_accent,
            scale,
        )
    elif stat_value:
        icon_box = (
            inner_right - small_icon_side,
            panel_box[3] - pad - small_icon_side,
            inner_right,
            panel_box[3] - pad,
        )
        value_font = _font(116 * scale, bold=True)
        max_value_width = inner_right - inner_left - small_icon_side - pad
        while pd.textbbox((0, 0), stat_value, font=value_font)[2] > max_value_width and value_font.size > int(44 * scale):
            value_font = _font(value_font.size - int(8 * scale), bold=True)
        pd.text((inner_left, content_top), stat_value, font=value_font, fill=(*theme_accent, 255))
        label_text = str(stat.get("label", "")).strip()
        if label_text:
            label_top = content_top + int(value_font.size * 1.18)
            _draw_fitted(
                pd,
                (inner_left, label_top),
                label_text,
                base_size=32 * scale,
                min_size=24 * scale,
                bold=False,
                fill=(226, 233, 245, 245),
                max_width=max_value_width,
                avail_height=max(0, panel_box[3] - pad - label_top),
                line_cap=3,
                spacing=int(4 * scale),
            )
        _draw_theme_icon(pd, icon_box, icon=theme["icon"], accent=theme_accent, scale=scale)
    elif points:
        icon_box = (
            inner_right - small_icon_side,
            panel_box[3] - pad - small_icon_side,
            inner_right,
            panel_box[3] - pad,
        )
        point_font = _font(31 * scale)
        bullet_size = int(14 * scale)
        text_left = inner_left + bullet_size + int(18 * scale)
        text_width = inner_right - text_left - small_icon_side - int(12 * scale)
        y_cursor = content_top
        for point in points:
            if y_cursor > panel_box[3] - int(56 * scale):
                break
            bullet_top = y_cursor + int(point_font.size * 0.36)
            pd.rounded_rectangle(
                (inner_left, bullet_top, inner_left + bullet_size, bullet_top + bullet_size),
                radius=int(4 * scale),
                fill=(*theme_accent, 255),
            )
            y_cursor = _draw_wrapped(
                pd,
                (text_left, y_cursor),
                point,
                point_font,
                (232, 238, 249, 248),
                text_width,
                2,
                int(2 * scale),
            )
            y_cursor += int(10 * scale)
        _draw_theme_icon(pd, icon_box, icon=theme["icon"], accent=theme_accent, scale=scale)
    else:
        insight = str(visual.get("brief") or visual.get("subject") or scene.get("body") or scene.get("headline") or "").strip()
        if insight:
            _draw_wrapped(
                pd,
                (inner_left, content_top),
                insight,
                _font(28 * scale),
                (224, 232, 244, 240),
                int((panel_box[2] - panel_box[0]) * 0.58),
                5,
                int(4 * scale),
            )
        icon_box = (
            int(panel_box[2] - (panel_box[3] - panel_box[1]) * 0.66),
            panel_box[1] + int(18 * scale),
            int(panel_box[2] - int(24 * scale)),
            panel_box[3] - int(18 * scale),
        )
        _draw_theme_icon(pd, icon_box, icon=theme["icon"], accent=theme_accent, scale=scale)


def _has_panel_content(visual: dict[str, Any]) -> bool:
    diagram = visual.get("diagram")
    stat = visual.get("stat")
    points = visual.get("points")
    return bool(
        (isinstance(diagram, dict) and str(diagram.get("type", "")) in DIAGRAM_TYPES)
        or (isinstance(stat, dict) and str(stat.get("value", "")).strip())
        or (isinstance(points, list) and any(str(pt).strip() for pt in points))
    )


def _create_scene_card_landscape(
    episode: dict[str, Any],
    scene: dict[str, Any],
    *,
    episode_dir: Path,
    out_path: Path,
    overlay_only: bool,
    background: str,
) -> None:
    """16:9 card for TV viewing (Boris 2026-08-01).

    Text and the data panel own the left column; the right column is a media
    window that shows a photo, a slideshow or a map WHOLE, never cropped, so a
    map and its numbers stay readable side by side. Captions run along the
    bottom across the full width.
    """
    width, height, _fps = episode_format(episode)
    layout = _card_layout(width, height)
    scale = layout.scale
    visual = scene.get("visual") or {}
    accent = _rgb(visual.get("accent", "#5DE1FF"))
    transparent = overlay_only or background == "transparent"
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0)) if transparent else _gradient_background(width, height, accent)

    if not transparent and visual.get("mode") == "image" and visual.get("image_path"):
        image_path = _resolve_local_asset(episode_dir, visual["image_path"], "image_path")
        with Image.open(image_path) as raw:
            backdrop = _cover(raw, (width, height))
        backdrop = ImageEnhance.Color(backdrop).enhance(0.70).filter(ImageFilter.GaussianBlur(max(1, width // 220)))
        shade = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shade, "RGBA")
        for y_line in range(height):
            alpha = int(105 + 95 * (y_line / max(height - 1, 1)))
            sd.line((0, y_line, width, y_line), fill=(4, 7, 14, min(alpha, 225)))
        canvas = Image.alpha_composite(backdrop.convert("RGBA"), shade)

    draw = ImageDraw.Draw(canvas, "RGBA")
    text_width = layout.text_right - layout.text_left
    _assets, _fit, media_in_window = _landscape_media_plan(scene, episode_dir)
    stage_holds_media = overlay_only and media_in_window

    # Where the data panel goes: the stage when nothing more informative claims
    # it, otherwise the left column beside the media.
    panel_in_stage = False
    if overlay_only:
        if not _has_panel_content(visual):
            panel_box = None
        elif stage_holds_media:
            panel_box = layout.side_panel
        else:
            panel_box = layout.stage
            panel_in_stage = True
    elif visual.get("mode", "template") in {"template", "veo"} or _has_panel_content(visual):
        panel_box = layout.stage
        panel_in_stage = True
    else:
        panel_box = None

    if transparent:
        # A scrim keeps the text column readable over footage or a moving
        # backdrop. Stage and side panel share the same bottom edge.
        scrim_bottom = layout.stage[3] + int(16 * scale)
        draw.rounded_rectangle(
            (layout.margin // 2, layout.margin // 2, layout.text_right + int(20 * scale), scrim_bottom),
            radius=int(38 * scale),
            fill=(5, 9, 17, 188),
        )

    _draw_card_chrome(draw, episode=episode, scene=scene, layout=layout, accent=accent)

    y = layout.text_top
    kicker = str(scene.get("kicker", "")).strip()
    if kicker:
        draw.text((layout.text_left, y), kicker.upper(), font=_font(26 * scale, bold=True), fill=(*accent, 255))
        y += int(48 * scale)
    text_limit = (layout.stage[3] if panel_box is None or panel_in_stage else panel_box[1]) - int(24 * scale)
    body = str(scene.get("body", "")).strip()
    body_reserve = int(110 * scale) if body else 0
    y = _draw_fitted(
        draw,
        (layout.text_left, y),
        str(scene.get("headline", "")),
        base_size=58 * scale,
        min_size=34 * scale,
        bold=True,
        fill=(247, 249, 253, 255),
        max_width=text_width,
        avail_height=max(0, text_limit - body_reserve - y),
        line_cap=4,
        spacing=int(7 * scale),
    )
    if body:
        y += int(22 * scale)
        _draw_fitted(
            draw,
            (layout.text_left, y),
            body,
            base_size=31 * scale,
            min_size=24 * scale,
            bold=False,
            fill=(207, 218, 234, 245),
            max_width=text_width,
            avail_height=max(0, text_limit - y),
            line_cap=4,
            spacing=int(6 * scale),
        )

    if panel_box is not None:
        panel = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        pd = ImageDraw.Draw(panel, "RGBA")
        _draw_insight_panel(pd, panel_box, scene=scene, visual=visual, scale=scale)
        canvas = Image.alpha_composite(canvas, panel)
        draw = ImageDraw.Draw(canvas, "RGBA")

    if stage_holds_media:
        # The window interior stays transparent: the fitted photo/map shows
        # through it. Only the frame is drawn, so nothing covers the media.
        stage = layout.stage
        draw.rounded_rectangle(
            (stage[0] - int(6 * scale), stage[1] - int(6 * scale), stage[2] + int(6 * scale), stage[3] + int(6 * scale)),
            radius=int(30 * scale),
            outline=(4, 8, 15, 170),
            width=max(2, int(6 * scale)),
        )
        draw.rounded_rectangle(
            stage,
            radius=int(26 * scale),
            outline=(*accent, 200),
            width=max(2, int(3 * scale)),
        )

    # Evidence/source chips: under the media window when there is one.
    sources_by_id = {source["id"]: source for source in episode.get("sources", [])}
    chip_x = layout.stage[0] if stage_holds_media else layout.text_left
    chip_font = _font(23 * scale, bold=True)
    for source_id in scene.get("source_ids", [])[:3]:
        label = _source_label(sources_by_id[source_id])[:28]
        chip_w = draw.textbbox((0, 0), label, font=chip_font)[2] + int(36 * scale)
        if chip_x + chip_w > width - layout.margin:
            break
        draw.rounded_rectangle(
            (chip_x, layout.chips_y, chip_x + chip_w, layout.chips_y + int(40 * scale)),
            radius=int(20 * scale),
            fill=(9, 18, 31, 245),
            outline=(*accent, 145),
            width=max(1, int(1 * scale)),
        )
        draw.text((chip_x + int(18 * scale), layout.chips_y + int(6 * scale)), label, font=chip_font, fill=(230, 237, 248, 245))
        chip_x += chip_w + int(12 * scale)

    # Burned-in caption band. The voice waveform shares the label row, so the
    # subtitle keeps the full height of the band.
    box_left, box_top, box_right, box_bottom = layout.caption_box
    draw.rounded_rectangle(
        (box_left, box_top, box_right, box_bottom),
        radius=int(30 * scale),
        fill=(5, 9, 17, 210),
        outline=(*accent, 115),
        width=max(2, int(2 * scale)),
    )
    draw.text(
        (box_left + int(30 * scale), box_top + int(18 * scale)),
        "ГОЛОС",
        font=_font(22 * scale, bold=True),
        fill=(*accent, 255),
    )
    caption_top = box_top + int(64 * scale)
    _draw_fitted(
        draw,
        (box_left + int(30 * scale), caption_top),
        _display_narration(str(scene.get("narration", "")).strip()) or str(scene.get("body", "")),
        base_size=32 * scale,
        min_size=23 * scale,
        bold=False,
        fill=(245, 247, 251, 255),
        max_width=(box_right - box_left) - int(60 * scale),
        avail_height=max(0, box_bottom - int(14 * scale) - caption_top),
        line_cap=4,
        spacing=int(6 * scale),
    )
    footer = "AI-голос · источники приложены"
    draw.text((layout.margin, layout.footer_y), footer, font=_font(22 * scale), fill=(174, 188, 207, 230))
    episode_mark = str(episode.get("episode_id", ""))[-18:]
    mark_font = _font(20 * scale)
    mark_w = draw.textbbox((0, 0), episode_mark, font=mark_font)[2]
    draw.text((width - layout.margin - mark_w, layout.footer_y), episode_mark, font=mark_font, fill=(129, 145, 168, 205))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, format="PNG", optimize=True)


def create_scene_card(
    episode: dict[str, Any],
    scene: dict[str, Any],
    *,
    episode_dir: Path,
    out_path: Path,
    overlay_only: bool = False,
    background: str = "gradient",
) -> None:
    width, height, _fps = episode_format(episode)
    if width > height:
        _create_scene_card_landscape(
            episode,
            scene,
            episode_dir=episode_dir,
            out_path=out_path,
            overlay_only=overlay_only,
            background=background,
        )
        return
    accent = _rgb((scene.get("visual") or {}).get("accent", "#5DE1FF"))
    transparent = overlay_only or background == "transparent"
    canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0)) if transparent else _gradient_background(width, height, accent)

    visual = scene.get("visual") or {}
    if not transparent and visual.get("mode") == "image" and visual.get("image_path"):
        image_path = _resolve_local_asset(episode_dir, visual["image_path"], "image_path")
        with Image.open(image_path) as raw:
            background = _cover(raw, (width, height))
        background = ImageEnhance.Color(background).enhance(0.70).filter(ImageFilter.GaussianBlur(max(1, width // 220)))
        shade = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shade, "RGBA")
        for y in range(height):
            alpha = int(115 + 105 * (y / max(height - 1, 1)))
            sd.line((0, y, width, y), fill=(4, 7, 14, min(alpha, 230)))
        canvas = Image.alpha_composite(background.convert("RGBA"), shade)

    draw = ImageDraw.Draw(canvas, "RGBA")
    margin = int(width * 0.075)
    content_width = width - margin * 2
    scale = width / 1080.0

    if overlay_only:
        draw.rounded_rectangle(
            (margin // 2, margin // 2, width - margin // 2, int(height * 0.54)),
            radius=int(42 * scale),
            fill=(5, 9, 17, 190),
        )
        draw.rounded_rectangle(
            (margin // 2, int(height * 0.66), width - margin // 2, height - margin // 2),
            radius=int(42 * scale),
            fill=(5, 9, 17, 210),
        )
    elif background == "transparent":
        # A soft scrim keeps the headline block readable over the moving backdrop.
        draw.rounded_rectangle(
            (margin // 2, margin // 2, width - margin // 2, int(height * 0.335)),
            radius=int(42 * scale),
            fill=(5, 9, 17, 175),
        )

    _draw_card_chrome(draw, episode=episode, scene=scene, layout=_card_layout(width, height), accent=accent)

    y = margin + int(112 * scale)
    kicker = str(scene.get("kicker", "")).strip()
    if kicker:
        draw.text((margin, y), kicker.upper(), font=_font(27 * scale, bold=True), fill=(*accent, 255))
        y += int(52 * scale)
    # The headline/body column ends where the next fixed-position block begins:
    # the insight panel for template/veo scenes, the caption scrim for Veo
    # overlays, or the source chips row when a full-bleed image is behind.
    panel_drawn = (
        not overlay_only and visual.get("mode", "template") in {"template", "veo"}
    ) or (overlay_only and _has_panel_content(visual))
    if overlay_only and not panel_drawn:
        text_limit = int(height * 0.54)
    elif panel_drawn:
        text_limit = int(height * (0.345 if _panel_has_diagram(visual) else 0.36))
    else:
        text_limit = int(height * 0.60)
    text_limit -= int(20 * scale)
    body = str(scene.get("body", "")).strip()
    body_reserve = int(140 * scale) if body else 0
    y = _draw_fitted(
        draw,
        (margin, y),
        str(scene.get("headline", "")),
        base_size=72 * scale,
        min_size=44 * scale,
        bold=True,
        fill=(247, 249, 253, 255),
        max_width=content_width,
        avail_height=max(0, text_limit - body_reserve - y),
        line_cap=5,
        spacing=int(8 * scale),
    )
    if body:
        y += int(30 * scale)
        _draw_fitted(
            draw,
            (margin, y),
            body,
            base_size=38 * scale,
            min_size=28 * scale,
            bold=False,
            fill=(207, 218, 234, 245),
            max_width=content_width,
            avail_height=max(0, text_limit - y),
            line_cap=6,
            spacing=int(7 * scale),
        )

    # The insight panel is the explanatory core for a visual learner. Priority:
    # a big concrete number (visual.stat) beats a bullet list (visual.points),
    # which beats the abstract brief+icon fallback. Image/Veo scenes supply
    # their own visual field and therefore skip it.
    # Photo-backed scenes still get their insight panel (Boris 2026-07-20:
    # «для всех тем должны быть картинки»): diagrams/stats/points draw over
    # broll/veo footage too. Only the abstract icon fallback stays reserved for
    # plain template cards, so footage is never covered without payoff.
    if (not overlay_only and visual.get("mode", "template") in {"template", "veo"}) or (
        overlay_only and _has_panel_content(visual)
    ):
        has_diagram = _panel_has_diagram(visual)
        panel = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        pd = ImageDraw.Draw(panel, "RGBA")
        panel_box = (
            margin,
            int(height * (0.345 if has_diagram else 0.36)),
            width - margin,
            int(height * (0.60 if has_diagram else 0.58)),
        )
        _draw_insight_panel(pd, panel_box, scene=scene, visual=visual, scale=scale)
        canvas = Image.alpha_composite(canvas, panel)
        draw = ImageDraw.Draw(canvas, "RGBA")

    # Evidence/source chips.
    sources_by_id = {source["id"]: source for source in episode.get("sources", [])}
    chip_y = int(height * 0.615)
    chip_x = margin
    chip_font = _font(23 * scale, bold=True)
    for source_id in scene.get("source_ids", [])[:3]:
        label = _source_label(sources_by_id[source_id])[:28]
        text_w = draw.textbbox((0, 0), label, font=chip_font)[2]
        chip_w = text_w + int(36 * scale)
        if chip_x + chip_w > width - margin:
            break
        draw.rounded_rectangle(
            (chip_x, chip_y, chip_x + chip_w, chip_y + int(46 * scale)),
            radius=int(23 * scale),
            fill=(9, 18, 31, 245),
            outline=(*accent, 145),
            width=max(1, int(1 * scale)),
        )
        draw.text((chip_x + int(18 * scale), chip_y + int(9 * scale)), label, font=chip_font, fill=(230, 237, 248, 245))
        chip_x += chip_w + int(12 * scale)

    # Burned-in caption card. This replaces libass/drawtext, unavailable in the
    # installed FFmpeg build, and guarantees captions on every rendered frame.
    narration = str(scene.get("narration", "")).strip()
    box_top = int(height * 0.69)
    box_bottom = height - margin - int(70 * scale)
    draw.rounded_rectangle(
        (margin, box_top, width - margin, box_bottom),
        radius=int(32 * scale),
        fill=(5, 9, 17, 205),
        outline=(*accent, 115),
        width=max(2, int(2 * scale)),
    )
    draw.text(
        (margin + int(30 * scale), box_top + int(25 * scale)),
        "ГОЛОС",
        font=_font(22 * scale, bold=True),
        fill=(*accent, 255),
    )
    _draw_fitted(
        draw,
        (margin + int(30 * scale), box_top + int(70 * scale)),
        _display_narration(narration) or str(scene.get("body", "")),
        base_size=34 * scale,
        min_size=25 * scale,
        bold=False,
        fill=(245, 247, 251, 255),
        max_width=content_width - int(60 * scale),
        avail_height=max(0, box_bottom - int(18 * scale) - (box_top + int(70 * scale))),
        line_cap=7,
        spacing=int(8 * scale),
    )
    footer = "AI-голос · источники приложены"
    draw.text((margin, height - margin - int(34 * scale)), footer, font=_font(22 * scale), fill=(174, 188, 207, 230))
    episode_mark = str(episode.get("episode_id", ""))[-18:]
    mark_w = draw.textbbox((0, 0), episode_mark, font=_font(20 * scale))[2]
    draw.text((width - margin - mark_w, height - margin - int(34 * scale)), episode_mark, font=_font(20 * scale), fill=(129, 145, 168, 205))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(out_path, format="PNG", optimize=True)


def _run(command: list[str], *, timeout: int = 600, capture: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=True,
            text=True,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise RenderError(f"required executable not found: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RenderError(f"command timed out after {timeout}s: {Path(command[0]).name}") from exc
    except subprocess.CalledProcessError as exc:
        stderr = (exc.stderr or "").strip()[-2000:]
        raise RenderError(f"{Path(command[0]).name} failed ({exc.returncode}): {stderr}") from exc


def probe_media(path: str | Path) -> dict[str, Any]:
    result = _run(
        [
            FFPROBE,
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=index,codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels",
            "-of",
            "json",
            str(path),
        ]
    )
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RenderError(f"ffprobe returned invalid JSON for {path}") from exc


def _duration(path: str | Path) -> float:
    info = probe_media(path)
    try:
        duration = float(info.get("format", {}).get("duration", 0) or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        for stream in info.get("streams", []):
            try:
                duration = max(duration, float(stream.get("duration", 0) or 0))
            except (TypeError, ValueError):
                pass
    return duration


def _silence(out_path: Path, seconds: float) -> None:
    _run(
        [
            FFMPEG,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=48000:cl=stereo",
            "-t",
            f"{max(seconds, 0.5):.3f}",
            "-c:a",
            "pcm_s16le",
            str(out_path),
        ],
        timeout=60,
    )


def _convert_audio(in_path: Path, out_path: Path, *, tempo: float = TTS_TEMPO) -> None:
    cmd = [FFMPEG, "-y", "-i", str(in_path)]
    if tempo and abs(tempo - 1.0) > 1e-3:
        cmd += ["-filter:a", f"atempo={tempo:g}"]
    cmd += ["-ar", "48000", "-ac", "2", "-c:a", "pcm_s16le", str(out_path)]
    _run(cmd, timeout=120)


def _tts_macos(text: str, out_path: Path, *, voice: str, rate: int, tempo: float = TTS_TEMPO) -> None:
    script = _narration_script(text)
    if not script:
        _silence(out_path, 0.75)
        return
    with tempfile.TemporaryDirectory(prefix="butler-news-video-tts-") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        parts: list[Path] = []
        speech_index = 0
        pause_index = 0
        for part in script:
            if part["kind"] == "text":
                raw = temp_dir / f"speech-{speech_index:03d}.aiff"
                out = temp_dir / f"speech-{speech_index:03d}.wav"
                _run([SAY, "-v", voice, "-r", str(rate), "-o", str(raw), str(part["text"])], timeout=180)
                if _duration(raw) <= 0.15:
                    raise RenderError(
                        "macOS say produced an empty audio file; run outside an app sandbox or choose --tts none/openai"
                    )
                _convert_audio(raw, out, tempo=tempo)
                raw.unlink(missing_ok=True)
                parts.append(out)
                speech_index += 1
            else:
                pause = temp_dir / f"pause-{pause_index:03d}.wav"
                _silence(pause, float(part.get("seconds", 0.0)))
                parts.append(pause)
                pause_index += 1
        _write_audio_concat(parts, out_path)


def _edge_rate_percent(rate: int) -> str:
    percent = int(round((rate / float(EDGE_TTS_BASELINE_WPM) - 1.0) * 100))
    return f"{max(-50, min(50, percent)):+d}%"


def _tts_edge(text: str, out_path: Path, *, voice: str, rate: int, tempo: float = TTS_TEMPO) -> None:
    """Neural narration through the free edge-tts voices (no API key, no billing).

    Pause/[breath] markers are honored the same way as for macOS: speech parts
    are synthesized separately and joined with measured silence.
    """
    try:
        import edge_tts
    except ImportError as exc:
        raise RenderError(f"edge-tts is not installed for {PYTHON}: {exc}") from exc
    import asyncio

    script = _narration_script(text)
    if not script:
        _silence(out_path, 0.75)
        return
    if not re.fullmatch(r"[a-z]{2,3}-[A-Za-z]{2,}-\w+", voice or ""):
        raise RenderError(f"{voice!r} is not an edge-tts voice name (expected e.g. {EDGE_TTS_DEFAULT_VOICE})")
    rate_percent = _edge_rate_percent(rate)

    async def _synthesize(part_text: str, target: Path) -> None:
        communicate = edge_tts.Communicate(part_text, voice, rate=rate_percent)
        await asyncio.wait_for(communicate.save(str(target)), timeout=120)

    with tempfile.TemporaryDirectory(prefix="butler-news-video-tts-") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        parts: list[Path] = []
        speech_index = 0
        pause_index = 0
        for part in script:
            if part["kind"] == "text":
                raw = temp_dir / f"speech-{speech_index:03d}.mp3"
                out = temp_dir / f"speech-{speech_index:03d}.wav"
                try:
                    asyncio.run(_synthesize(str(part["text"]), raw))
                except Exception as exc:
                    raise RenderError(f"edge-tts synthesis failed (network?): {exc}") from exc
                if not raw.is_file() or _duration(raw) <= 0.15:
                    raise RenderError("edge-tts produced an empty audio file")
                _convert_audio(raw, out, tempo=tempo)
                raw.unlink(missing_ok=True)
                parts.append(out)
                speech_index += 1
            else:
                pause = temp_dir / f"pause-{pause_index:03d}.wav"
                _silence(pause, float(part.get("seconds", 0.0)))
                parts.append(pause)
                pause_index += 1
        _write_audio_concat(parts, out_path)


def _openai_key_from_openclaw_config() -> str | None:
    config = Path("~/.openclaw/openclaw.json").expanduser()
    try:
        data = json.loads(config.read_text(encoding="utf-8"))
        key = data["skills"]["entries"]["openai-whisper-api"]["apiKey"]
        return str(key).strip() or None
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _tts_openai(
    text: str,
    out_path: Path,
    *,
    voice: str,
    approval: str | None,
    max_usd: float | None,
    tempo: float = TTS_TEMPO,
) -> None:
    _paid_budget_authorized(
        stage="OpenAI TTS",
        approval=approval,
        max_usd=max_usd,
        estimate_usd=0.01,
    )
    if not os.environ.get("OPENAI_API_KEY"):
        # Dispatch workers strip OPENAI_API_KEY from their env (codex auth
        # hygiene), so fall back to the same key the whisper transcriber uses.
        key = _openai_key_from_openclaw_config()
        if not key:
            raise RenderError(
                "no OpenAI API key (env OPENAI_API_KEY or openclaw.json "
                "skills.entries.openai-whisper-api.apiKey); no paid TTS call was made"
            )
        os.environ["OPENAI_API_KEY"] = key
    raw = out_path.with_suffix(".openai.wav")
    narration = re.sub(NARRATION_MARKER_RE, "", text).strip()
    if not narration:
        _silence(out_path, 0.75)
        return
    instructions = (
        "Voice Affect: Warm, human, and steady. Tone: Clear, factual, and confident. "
        "Pacing: Moderate, with natural sentence breaks and a slight inhale before new sections. "
        "Read punctuation naturally and avoid sounding clipped or robotic."
    )
    _run(
        [
            PYTHON,
            OPENAI_TTS_CLI,
            "speak",
            "--input",
            narration,
            "--voice",
            voice,
            "--instructions",
            instructions,
            "--response-format",
            "wav",
            "--out",
            str(raw),
        ],
        timeout=300,
    )
    _convert_audio(raw, out_path, tempo=tempo)
    raw.unlink(missing_ok=True)


def estimate_openai_tts_cost(episode: dict[str, Any]) -> float:
    seconds = 0.0
    for scene in episode.get("scenes", []):
        narration = str(scene.get("narration", "")).strip()
        if not narration:
            continue
        hint = float(scene.get("duration_hint", 0) or 0)
        seconds += max(hint, estimate_narration_seconds(narration), 2.0) + 0.45
    if seconds <= 0:
        return 0.0
    raw = seconds / 60.0 * OPENAI_TTS_ESTIMATED_USD_PER_MINUTE
    return math.ceil(raw * 100.0) / 100.0


def make_scene_audio(
    scene: dict[str, Any],
    out_path: Path,
    *,
    tts: str,
    voice: str,
    rate: int,
    approval: str | None,
    max_usd: float | None,
    tempo: float = TTS_TEMPO,
) -> tuple[float, list[str]]:
    narration = str(scene.get("narration", "")).strip()
    hint = float(scene.get("duration_hint", 0) or 0)
    warnings: list[str] = []
    if not narration or tts == "none":
        seconds = max(hint, estimate_narration_seconds(narration), 2.0)
        _silence(out_path, seconds)
        if narration and tts == "none":
            warnings.append(f"scene {scene['id']}: caption-only; TTS disabled")
        return seconds, warnings
    try:
        if tts == "edge":
            try:
                _tts_edge(narration, out_path, voice=voice, rate=rate, tempo=tempo)
            except RenderError as exc:
                warnings.append(
                    f"scene {scene['id']}: edge TTS failed, fell back to macOS {MACOS_FALLBACK_VOICE}: {exc}"
                )
                _tts_macos(narration, out_path, voice=MACOS_FALLBACK_VOICE, rate=rate, tempo=tempo)
        elif tts == "macos":
            _tts_macos(narration, out_path, voice=voice, rate=rate, tempo=tempo)
        elif tts == "openai":
            try:
                _tts_openai(narration, out_path, voice=voice, approval=approval, max_usd=max_usd, tempo=tempo)
            except RenderError as exc:
                # A release must never lose its voice to a paid-API hiccup:
                # degrade to the free neural voice (which itself degrades to
                # the offline macOS voice) and record the downgrade.
                warnings.append(
                    f"scene {scene['id']}: OpenAI TTS failed, fell back to edge {EDGE_TTS_DEFAULT_VOICE}: {exc}"
                )
                try:
                    _tts_edge(narration, out_path, voice=EDGE_TTS_DEFAULT_VOICE, rate=rate, tempo=tempo)
                except RenderError as edge_exc:
                    warnings.append(
                        f"scene {scene['id']}: edge TTS failed, fell back to macOS {MACOS_FALLBACK_VOICE}: {edge_exc}"
                    )
                    _tts_macos(narration, out_path, voice=MACOS_FALLBACK_VOICE, rate=rate, tempo=tempo)
        else:
            raise RenderError(f"unsupported TTS provider: {tts}")
    except PaidAPIApprovalRequired:
        raise
    except RenderError as exc:
        seconds = max(hint, estimate_narration_seconds(narration), 2.0)
        _silence(out_path, seconds)
        warnings.append(f"scene {scene['id']}: TTS failed, used caption-only fallback: {exc}")
        return seconds, warnings
    seconds = max(_duration(out_path) + 0.45, hint, 2.0)
    return seconds, warnings


def _kenburns_clip(image: Path, out_path: Path, *, width: int, height: int, fps: int) -> Path:
    """Slow pan/zoom clip from a still so photo scenes read as footage."""
    if out_path.is_file():
        return out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    duration = 12.0
    frames = max(1, math.ceil(duration * fps))
    vf = (
        f"scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase,"
        f"crop={width * 2}:{height * 2},"
        f"zoompan=z='min(zoom+0.0009,1.12)':x='iw/2-(iw/zoom/2)':y='ih/2.3-(ih/zoom/2.3)':"
        f"d={frames}:s={width}x{height}:fps={fps},format=yuv420p"
    )
    _run(
        [FFMPEG, "-y", "-loop", "1", "-framerate", str(fps), "-i", str(image),
         "-t", f"{duration:.2f}", "-vf", vf, "-an",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", str(out_path)],
        timeout=300,
    )
    return out_path


def _scene_broll_assets(scene: dict[str, Any], episode_dir: Path) -> list[Path]:
    """Every still/clip declared for a b-roll scene, in display order."""
    visual = scene.get("visual") or {}
    declared = visual.get("broll_paths")
    if not isinstance(declared, list) or not declared:
        declared = [visual["broll_path"]] if visual.get("broll_path") else []
    assets: list[Path] = []
    for item in declared[:MAX_SLIDESHOW_IMAGES]:
        asset = _resolve_local_asset(episode_dir, str(item), f"scene {scene['id']} broll_path")
        if asset.is_file():
            assets.append(asset)
    return assets


def _media_fit(visual: dict[str, Any], assets: list[Path]) -> str:
    """How the media fills its window. Maps default to ``contain`` because a
    cropped map is useless (Boris 2026-08-01: «карту показывать полностью»)."""
    declared = visual.get("fit")
    if isinstance(declared, str) and declared in ALLOWED_VISUAL_FITS:
        return declared
    if any(asset.parent.name.lower() == "maps" for asset in assets):
        return "contain"
    return "cover"


def _landscape_media_plan(scene: dict[str, Any], episode_dir: Path) -> tuple[list[Path], str, bool]:
    """Who gets the stage on a 16:9 card: the media window or the data panel.

    A map (``fit=contain``) always takes the window — it must be readable whole,
    with its numbers beside it. A decorative photo yields the stage to a real
    diagram and becomes the full-frame backdrop instead, because a cramped chart
    explains nothing. Returns ``(assets, fit, media_in_window)``.
    """
    visual = scene.get("visual") or {}
    if visual.get("mode") != "broll":
        return [], "cover", False
    assets = _scene_broll_assets(scene, episode_dir)
    fit = _media_fit(visual, assets)
    in_window = bool(assets) and (fit == "contain" or not _panel_has_diagram(visual))
    return assets, fit, in_window


def _slide_plan(count: int, duration: float) -> tuple[int, float]:
    """How many stills actually fit in ``duration`` and how long each one runs.

    Slides overlap by ``SLIDE_CROSSFADE_SECONDS``; a slide shorter than
    ``MIN_SLIDE_SECONDS`` would read as a flicker rather than a picture change.
    """
    total = max(duration, MIN_SLIDE_SECONDS)
    room = int((total - SLIDE_CROSSFADE_SECONDS) / (MIN_SLIDE_SECONDS - SLIDE_CROSSFADE_SECONDS))
    slides = max(1, min(count, MAX_SLIDESHOW_IMAGES, room))
    slide_seconds = (total + (slides - 1) * SLIDE_CROSSFADE_SECONDS) / slides
    return slides, slide_seconds


def _media_window_clip(
    images: list[Path],
    out_path: Path,
    *,
    width: int,
    height: int,
    fps: int,
    duration: float,
    fit: str,
) -> Path:
    """Footage for the media window: one still, or a slideshow that changes the
    picture mid-scene (Boris 2026-08-01: «должна отображаться сменяющаяся
    картинка»). ``contain`` keeps a map or chart whole; ``cover`` fills the
    window with a slow Ken Burns move."""
    total = max(duration + 0.6, MIN_SLIDE_SECONDS)
    slides, slide_seconds = _slide_plan(len(images), total)
    used = images[:slides]
    # Cache key covers the stills themselves: swapping a photo without touching
    # episode.json used to re-use the previous clip (L-2026-07-20-kenburns-cache).
    fingerprint = hashlib.sha256(
        "|".join(
            f"{path.name}:{path.stat().st_size}:{path.stat().st_mtime_ns}" for path in used
        ).encode("utf-8")
        + f"|{fit}|{slides}|{slide_seconds:.3f}".encode("utf-8")
    ).hexdigest()[:10]
    out_path = out_path.with_name(f"{out_path.stem}-{fingerprint}{out_path.suffix}")
    if out_path.is_file() and _duration(out_path) >= total - 0.3:
        return out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    frames = max(1, math.ceil(slide_seconds * fps))
    command: list[str] = [FFMPEG, "-y"]
    for image in used:
        command += ["-loop", "1", "-framerate", str(fps), "-t", f"{slide_seconds:.3f}", "-i", str(image)]
    steps: list[str] = []
    for index in range(slides):
        if fit == "cover":
            prep = (
                f"scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase,"
                f"crop={width * 2}:{height * 2},"
                f"zoompan=z='min(zoom+0.0009,1.10)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
                f"d={frames}:s={width}x{height}:fps={fps}"
            )
        else:
            prep = (
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x0A1018,fps={fps}"
            )
        steps.append(
            f"[{index}:v]{prep},trim=duration={slide_seconds:.3f},setpts=PTS-STARTPTS,format=yuv420p[v{index}]"
        )
    last = "v0"
    for index in range(1, slides):
        offset = index * (slide_seconds - SLIDE_CROSSFADE_SECONDS)
        label = f"x{index}"
        steps.append(
            f"[{last}][v{index}]xfade=transition=fade:duration={SLIDE_CROSSFADE_SECONDS:g}:"
            f"offset={offset:.3f}[{label}]"
        )
        last = label
    command += [
        "-filter_complex",
        ";".join(steps),
        "-map",
        f"[{last}]",
        "-t",
        f"{total:.3f}",
        "-an",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "21",
        "-pix_fmt",
        "yuv420p",
        str(out_path),
    ]
    _run(command, timeout=max(300, int(total * 20)))
    return out_path


def _render_windowed_scene(
    clip: Path,
    card: Path,
    audio: Path,
    out_path: Path,
    *,
    duration: float,
    width: int,
    height: int,
    fps: int,
    window: tuple[int, int, int, int] | None,
    fit: str,
    accent_hex: str,
) -> None:
    """Landscape scene composited over a blurred, darkened frame-filling backdrop.

    With a ``window`` the media is also shown sharp inside it (whole when
    ``fit=contain``). Without one the diagram owns the stage and the footage
    stays a backdrop — at full brightness it would swallow the panel and the
    headline.
    """
    fade_out = max(0.0, duration - 0.25)
    wave_x, wave_y, wave_w, wave_h = _caption_wave_geometry(width, height)
    accent = "0x%02X%02X%02X" % _rgb(accent_hex)
    backdrop = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,crop={width}:{height},"
        f"gblur=sigma={max(8, width // 60)},eq=brightness=-0.18:saturation=0.55,fps={fps}"
    )
    if window:
        x0, y0, x1, y1 = window
        win_w, win_h = x1 - x0, y1 - y0
        crop_fg = f"crop={win_w}:{win_h}," if fit == "cover" else ""
        stage = (
            f"[0:v]split=2[wbg][wfg];"
            f"[wbg]{backdrop}[bg];"
            f"[wfg]scale={win_w}:{win_h}:force_original_aspect_ratio="
            f"{'increase' if fit == 'cover' else 'decrease'},{crop_fg}fps={fps}[fg];"
            f"[bg][fg]overlay=x={x0}+({win_w}-overlay_w)/2:y={y0}+({win_h}-overlay_h)/2:shortest=1[base];"
        )
    else:
        stage = f"[0:v]{backdrop}[base];"
    filter_complex = (
        f"{stage}"
        f"[1:v]scale={width}:{height},format=rgba,fade=t=in:st=0:d=0.35:alpha=1[card];"
        f"[base][card]overlay=0:0[framed];"
        f"[2:a]pan=mono|c0=0.5*c0+0.5*c1,"
        # draw=full: the default additive draw accumulates overlapping samples
        # in YUV and shifts the accent hue toward dirty yellow.
        f"showwaves=s={wave_w}x{wave_h}:mode=cline:rate={fps}:draw=full:colors={accent},"
        f"format=rgba,colorkey=0x000000:0.08:0.0[wave];"
        f"[framed][wave]overlay={wave_x}:{wave_y}:format=auto,"
        f"fade=t=in:st=0:d=0.20,fade=t=out:st={fade_out:.3f}:d=0.25,format=yuv420p[v]"
    )
    _run(
        [
            FFMPEG,
            "-y",
            "-stream_loop",
            "-1",
            "-i",
            str(clip),
            "-loop",
            "1",
            "-framerate",
            str(fps),
            "-i",
            str(card),
            "-i",
            str(audio),
            "-t",
            f"{duration:.3f}",
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-map",
            "2:a:0",
            "-af",
            "loudnorm=I=-16:TP=-1:LRA=11",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(out_path),
        ],
        timeout=max(240, int(duration * 12)),
    )


def _video_asset_for_scene(scene: dict[str, Any], episode_dir: Path, run_dir: Path) -> Path | None:
    visual = scene.get("visual") or {}
    if visual.get("mode") == "broll":
        # Stills are converted to Ken Burns clips by the render loop, which
        # knows the episode's frame geometry.
        assets = _scene_broll_assets(scene, episode_dir)
        return assets[0] if assets else None
    if visual.get("mode") != "veo":
        return None
    explicit = visual.get("veo_asset")
    if explicit:
        candidate = _resolve_local_asset(episode_dir, str(explicit), f"scene {scene['id']} veo_asset")
        return candidate if candidate.is_file() else None
    generated = run_dir / "assets" / "veo" / f"{scene['id']}.mp4"
    return generated if generated.is_file() else None


def _render_static_scene(
    card: Path,
    audio: Path,
    out_path: Path,
    *,
    duration: float,
    width: int,
    height: int,
    fps: int,
) -> None:
    fade_out = max(0.0, duration - 0.25)
    frames = max(1, math.ceil(duration * fps))
    vf = (
        f"scale={width}:{height},"
        f"zoompan=z='min(zoom+0.00010,1.018)':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"d={frames}:s={width}x{height}:fps={fps},"
        f"fade=t=in:st=0:d=0.20,fade=t=out:st={fade_out:.3f}:d=0.25,format=yuv420p"
    )
    _run(
        [
            FFMPEG,
            "-y",
            "-loop",
            "1",
            "-framerate",
            str(fps),
            "-i",
            str(card),
            "-i",
            str(audio),
            "-t",
            f"{duration:.3f}",
            "-vf",
            vf,
            "-af",
            "loudnorm=I=-16:TP=-1:LRA=11",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(out_path),
        ],
        timeout=max(180, int(duration * 8)),
    )


def _render_video_scene(
    clip: Path,
    overlay: Path,
    audio: Path,
    out_path: Path,
    *,
    duration: float,
    width: int,
    height: int,
    fps: int,
) -> None:
    fade_out = max(0.0, duration - 0.25)
    filter_complex = (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},fps={fps},trim=duration={duration:.3f},setpts=PTS-STARTPTS[bg];"
        f"[1:v]scale={width}:{height},format=rgba[ov];"
        f"[bg][ov]overlay=0:0:shortest=1,"
        f"fade=t=in:st=0:d=0.20,fade=t=out:st={fade_out:.3f}:d=0.25,format=yuv420p[v]"
    )
    _run(
        [
            FFMPEG,
            "-y",
            "-stream_loop",
            "-1",
            "-i",
            str(clip),
            "-loop",
            "1",
            "-framerate",
            str(fps),
            "-i",
            str(overlay),
            "-i",
            str(audio),
            "-t",
            f"{duration:.3f}",
            "-filter_complex",
            filter_complex,
            "-map",
            "[v]",
            "-map",
            "2:a:0",
            "-af",
            "loudnorm=I=-16:TP=-1:LRA=11",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "20",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-movflags",
            "+faststart",
            str(out_path),
        ],
        timeout=max(180, int(duration * 8)),
    )


def _srt_timestamp(seconds: float) -> str:
    millis = max(0, round(seconds * 1000))
    hours, millis = divmod(millis, 3_600_000)
    minutes, millis = divmod(millis, 60_000)
    secs, millis = divmod(millis, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def _write_srt(entries: list[dict[str, Any]], out_path: Path) -> None:
    chunks: list[str] = []
    for index, entry in enumerate(entries, start=1):
        text = str(entry.get("narration", "")).strip()
        if not text:
            continue
        chunks.append(
            f"{index}\n{_srt_timestamp(entry['start'])} --> {_srt_timestamp(entry['end'])}\n{text}\n"
        )
    out_path.write_text("\n".join(chunks), encoding="utf-8")


def _write_credits(episode: dict[str, Any], out_path: Path) -> None:
    lines = [
        f"# {episode['title']} — источники",
        "",
        f"Episode: `{episode['episode_id']}`",
        f"As of: {episode['as_of']}",
        "",
        "> Озвучка создана искусственным интеллектом или системным синтезатором речи.",
        "> Генеративные Veo-вставки, если использованы, являются декоративными и не служат доказательством события.",
        "",
        "## Sources",
        "",
    ]
    for source in episode.get("sources", []):
        title = source.get("title") or source["id"]
        publisher = source.get("publisher") or ""
        url = source.get("url") or ""
        suffix = f" — {url}" if url else ""
        lines.append(f"- **{publisher or title}**: {title}{suffix}")
    lines.extend(["", "## Coverage limitations", ""])
    for limitation in episode.get("coverage", {}).get("limitations", []):
        lines.append(f"- {limitation}")
    out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _concat_scenes(scene_paths: list[Path], out_path: Path) -> None:
    concat_file = out_path.parent / "concat.txt"
    # Paths are generated under the run directory and contain no user-supplied
    # quoting. Still escape single quotes for concat-demuxer correctness.
    lines = ["file '" + str(path).replace("'", "'\\''") + "'" for path in scene_paths]
    concat_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temp_out = out_path.with_suffix(".tmp.mp4")
    _run(
        [
            FFMPEG,
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_file),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(temp_out),
        ],
        timeout=600,
    )
    os.replace(temp_out, out_path)


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RenderError(f"invalid {label}: {path}") from exc
    if not isinstance(value, dict):
        raise RenderError(f"{label} must contain a JSON object: {path}")
    return value


def _assert_run_identity(run_dir: Path, episode_id: str, episode_sha256: str) -> None:
    """Prove an existing non-empty directory belongs to this renderer/run."""
    marker_path = run_dir / RUN_MARKER
    episode_path = run_dir / "episode.json"
    run_path = run_dir / "run.json"
    if marker_path.is_file():
        marker = _read_json_object(marker_path, "news-video run marker")
        if marker.get("episode_id") != episode_id:
            raise RenderError(f"run directory belongs to another episode: {run_dir}")
        marker_digest = marker.get("episode_sha256")
        if marker_digest and marker_digest != episode_sha256:
            raise RenderError(f"run directory contains a different revision of episode {episode_id}")
        return

    # Legacy runs created before the marker was introduced are accepted only
    # when all three strong artifacts exist and agree on the same episode.
    if not ((run_dir / "SUCCESS").is_file() and episode_path.is_file() and run_path.is_file()):
        raise RenderError(
            f"refusing to modify non-empty directory without a news-video marker: {run_dir}"
        )
    existing_episode = _read_json_object(episode_path, "legacy episode.json")
    existing_run = _read_json_object(run_path, "legacy run.json")
    if existing_episode.get("episode_id") != episode_id or existing_run.get("episode_id") != episode_id:
        raise RenderError(f"legacy run directory belongs to another episode: {run_dir}")
    if _episode_digest(existing_episode) != episode_sha256:
        raise RenderError(f"legacy run directory contains a different revision of episode {episode_id}")


def _safe_clear_run_dir(run_dir: Path, episode_id: str, episode_sha256: str) -> None:
    protected = {
        Path("/").resolve(),
        Path.home().resolve(),
        DEFAULT_WORKSPACE.resolve(),
        DEFAULT_WORKSPACE.parent.resolve(),
        RUNS_ROOT.expanduser().resolve(),
    }
    if run_dir in protected or len(run_dir.parts) < 4:
        raise RenderError(f"refusing to overwrite a protected/broad directory: {run_dir}")
    if not run_dir.exists():
        return
    if not any(run_dir.iterdir()):
        run_dir.rmdir()
        return
    _assert_run_identity(run_dir, episode_id, episode_sha256)
    publish_receipt = run_dir / "publish" / "telegram.json"
    if publish_receipt.is_file():
        receipt = _read_json_object(publish_receipt, "publish receipt")
        if receipt.get("status") == "published":
            raise RenderError(f"refusing to overwrite a published run: {run_dir}")

    def _remove(path: Path) -> None:
        if not path.exists() and not path.is_symlink():
            return
        if path.is_symlink() or path.is_file():
            path.unlink()
            return
        shutil.rmtree(path)

    for path in (
        run_dir / RUN_MARKER,
        run_dir / "SUCCESS",
        run_dir / "episode.json",
        run_dir / "run.json",
        run_dir / "captions.srt",
        run_dir / "credits.md",
        run_dir / "publish",
        run_dir / "render",
        run_dir / "assets" / "cards",
        run_dir / "assets" / "audio",
    ):
        _remove(path)


def _load_completed_run(run_dir: Path, episode: dict[str, Any], episode_sha256: str) -> dict[str, Any]:
    final_video = run_dir / "render" / "episode.mp4"
    run_path = run_dir / "run.json"
    copied_episode_path = run_dir / "episode.json"
    for required in (run_path, copied_episode_path, final_video):
        if not required.is_file():
            raise RenderError(f"SUCCESS run is incomplete; missing {required}")
    existing_episode = _read_json_object(copied_episode_path, "completed episode.json")
    result = _read_json_object(run_path, "completed run.json")
    if existing_episode.get("episode_id") != episode.get("episode_id") or result.get("episode_id") != episode.get("episode_id"):
        raise RenderError(f"output directory already contains another episode: {run_dir}")
    existing_digest = _episode_digest(existing_episode)
    stored_digest = result.get("episode_sha256") or existing_digest
    if existing_digest != episode_sha256 or stored_digest != episode_sha256:
        raise RenderError(
            f"episode {episode['episode_id']} already rendered from different content; choose a new episode_id"
        )
    actual_video_sha = hashlib.sha256(final_video.read_bytes()).hexdigest()
    if result.get("sha256") != actual_video_sha:
        raise RenderError("completed video checksum does not match run.json")
    return result


def render_episode(
    episode_path: str | Path,
    *,
    output_dir: str | Path | None = None,
    tts: str = "edge",
    voice: str | None = None,
    rate: int = 175,
    overwrite: bool = False,
    paid_api_approval: str | None = None,
    max_usd: float | None = None,
) -> dict[str, Any]:
    episode, resolved_episode_path = load_episode(episode_path)
    warnings = validate_episode(episode, episode_path=resolved_episode_path, strict=True)
    if tts not in ALLOWED_TTS_PROVIDERS:
        raise RenderError(f"unsupported TTS provider: {tts}")
    voice = voice or DEFAULT_TTS_VOICES.get(tts, EDGE_TTS_DEFAULT_VOICE)
    if not isinstance(rate, int) or not (80 <= rate <= 400):
        raise ValidationError("TTS rate must be an integer between 80 and 400 words/minute")
    tts_estimate = 0.0
    if tts == "openai":
        tts_estimate = estimate_openai_tts_cost(episode)
        if tts_estimate > 0:
            _paid_budget_authorized(
                stage="OpenAI TTS episode",
                approval=paid_api_approval,
                max_usd=max_usd,
                estimate_usd=tts_estimate,
            )
    episode_sha256 = _episode_digest(episode)
    run_dir = Path(output_dir).expanduser().resolve() if output_dir else (RUNS_ROOT / episode["episode_id"]).resolve()
    success_file = run_dir / "SUCCESS"
    final_video = run_dir / "render" / "episode.mp4"
    with _locked_run_dir(run_dir):
        if success_file.is_file() and not overwrite:
            return _load_completed_run(run_dir, episode, episode_sha256)
        if run_dir.exists() and overwrite:
            _safe_clear_run_dir(run_dir, episode["episode_id"], episode_sha256)
        elif run_dir.exists() and any(run_dir.iterdir()):
            _assert_run_identity(run_dir, episode["episode_id"], episode_sha256)

        cards_dir = run_dir / "assets" / "cards"
        audio_dir = run_dir / "assets" / "audio"
        scenes_dir = run_dir / "render" / "scenes"
        for directory in (cards_dir, audio_dir, scenes_dir, run_dir / "render"):
            directory.mkdir(parents=True, exist_ok=True)
        _atomic_write_json(
            run_dir / RUN_MARKER,
            {
                "schema_version": "butler.news-video-run/v1",
                "episode_id": episode["episode_id"],
                "episode_sha256": episode_sha256,
                "created_at": _now_iso(),
            },
        )
        _atomic_write_json(run_dir / "episode.json", episode)

        width, height, fps = episode_format(episode)
        layout = _card_layout(width, height)
        tempo = episode_tempo(episode)
        timeline: list[dict[str, Any]] = []
        scene_videos: list[Path] = []
        cursor = 0.0
        for scene in episode["scenes"]:
            scene_id = scene["id"]
            audio_path = audio_dir / f"{scene_id}.wav"
            duration, audio_warnings = make_scene_audio(
                scene,
                audio_path,
                tts=tts,
                voice=voice,
                rate=rate,
                approval=paid_api_approval,
                max_usd=max_usd,
                tempo=tempo,
            )
            warnings.extend(audio_warnings)
            visual = scene.get("visual") or {}
            window: tuple[int, int, int, int] | None = None
            fit = "cover"
            # Landscape b-roll always composites over a blurred backdrop, with or
            # without a sharp media window; Veo brand clips stay full-bleed.
            backdrop_scene = False
            clip = _video_asset_for_scene(scene, resolved_episode_path.parent, run_dir)
            if layout.landscape and visual.get("mode") == "broll" and clip:
                # Landscape b-roll either fills the card's media window — whole
                # for maps, slowly panned for photos — or, when a diagram claims
                # the stage, becomes the full-frame backdrop. Either way the
                # picture may change mid-scene.
                assets, fit, media_in_window = _landscape_media_plan(scene, resolved_episode_path.parent)
                stills = [asset for asset in assets if asset.suffix.lower() in IMAGE_SUFFIXES]
                window = layout.stage if media_in_window else None
                backdrop_scene = True
                clip_width = (window[2] - window[0]) if window else width
                clip_height = (window[3] - window[1]) if window else height
                if stills:
                    clip = _media_window_clip(
                        stills,
                        run_dir / "assets" / "broll" / f"{scene_id}.mp4",
                        width=clip_width,
                        height=clip_height,
                        fps=fps,
                        duration=duration,
                        fit=fit if window else "cover",
                    )
            elif clip and clip.suffix.lower() in IMAGE_SUFFIXES:
                clip = _kenburns_clip(
                    clip,
                    run_dir / "assets" / "broll" / f"{scene_id}.mp4",
                    width=width,
                    height=height,
                    fps=fps,
                )
            card_path = cards_dir / f"{scene_id}.png"
            create_scene_card(
                episode,
                scene,
                episode_dir=resolved_episode_path.parent,
                out_path=card_path,
                overlay_only=bool(clip),
            )
            scene_video = scenes_dir / f"{len(scene_videos):02d}-{scene_id}.mp4"
            if clip:
                try:
                    clip_info = probe_media(clip)
                    video_stream = next((s for s in clip_info.get("streams", []) if s.get("codec_type") == "video"), None)
                    if not video_stream:
                        raise RenderError("no video stream")
                    if backdrop_scene:
                        _render_windowed_scene(
                            clip,
                            card_path,
                            audio_path,
                            scene_video,
                            duration=duration,
                            width=width,
                            height=height,
                            fps=fps,
                            window=window,
                            fit=fit,
                            accent_hex=str(visual.get("accent", "#5DE1FF")),
                        )
                    else:
                        _render_video_scene(
                            clip,
                            card_path,
                            audio_path,
                            scene_video,
                            duration=duration,
                            width=width,
                            height=height,
                            fps=fps,
                        )
                except RenderError as exc:
                    warnings.append(f"scene {scene_id}: footage unusable, used graphic fallback: {exc}")
                    create_scene_card(
                        episode,
                        scene,
                        episode_dir=resolved_episode_path.parent,
                        out_path=card_path,
                        overlay_only=False,
                    )
                    _render_static_scene(
                        card_path,
                        audio_path,
                        scene_video,
                        duration=duration,
                        width=width,
                        height=height,
                        fps=fps,
                    )
            else:
                if (scene.get("visual") or {}).get("mode") == "veo":
                    warnings.append(f"scene {scene_id}: Veo asset absent, used graphic fallback")
                rendered_motion = False
                if (scene.get("visual") or {}).get("mode") != "image":
                    # Living background + voice waveform; any failure falls back
                    # to the plain static card so the release is never lost.
                    try:
                        style = _motion_style(scene)
                        loop = _ensure_motion_loop(style, width=width, height=height, fps=fps)
                        create_scene_card(
                            episode,
                            scene,
                            episode_dir=resolved_episode_path.parent,
                            out_path=card_path,
                            background="transparent",
                        )
                        _render_motion_scene(
                            loop,
                            card_path,
                            audio_path,
                            scene_video,
                            duration=duration,
                            width=width,
                            height=height,
                            fps=fps,
                            accent_hex=str((scene.get("visual") or {}).get("accent", "#5DE1FF")),
                        )
                        rendered_motion = True
                    except (RenderError, OSError) as exc:
                        warnings.append(f"scene {scene_id}: motion background failed, used static card: {exc}")
                        create_scene_card(
                            episode,
                            scene,
                            episode_dir=resolved_episode_path.parent,
                            out_path=card_path,
                        )
                if not rendered_motion:
                    _render_static_scene(
                        card_path,
                        audio_path,
                        scene_video,
                        duration=duration,
                        width=width,
                        height=height,
                        fps=fps,
                    )
            entry = {
                "scene_id": scene_id,
                "start": round(cursor, 3),
                "end": round(cursor + duration, 3),
                "duration": round(duration, 3),
                "narration": _display_narration(scene.get("narration", "")),
                "video": str(scene_video.relative_to(run_dir)),
            }
            timeline.append(entry)
            cursor += duration
            scene_videos.append(scene_video)

        _concat_scenes(scene_videos, final_video)
        _write_srt(timeline, run_dir / "captions.srt")
        _write_credits(episode, run_dir / "credits.md")
        (run_dir / "render" / "timeline.json").write_text(
            json.dumps(timeline, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        poster = run_dir / "render" / "poster.jpg"
        _run(
            [FFMPEG, "-y", "-ss", "0.30", "-i", str(final_video), "-frames:v", "1", "-q:v", "2", str(poster)],
            timeout=120,
        )
        sha256 = hashlib.sha256(final_video.read_bytes()).hexdigest()
        (run_dir / "render" / "episode.sha256").write_text(f"{sha256}  episode.mp4\n", encoding="utf-8")
        media = probe_media(final_video)
        result = {
            "episode_id": episode["episode_id"],
            "episode_sha256": episode_sha256,
            "profile": episode["profile"],
            "status": "rendered",
            "rendered_at": _now_iso(),
            "run_dir": str(run_dir),
            "video": str(final_video),
            "poster": str(poster),
            "captions": str(run_dir / "captions.srt"),
            "credits": str(run_dir / "credits.md"),
            "duration_seconds": round(_duration(final_video), 3),
            "sha256": sha256,
            "warnings": warnings,
            "media": media,
            "tts_provider": tts,
            "voice": voice,
            "estimated_tts_cost_usd": tts_estimate,
        }
        (run_dir / "run.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        success_file.write_text(_now_iso() + "\n", encoding="utf-8")
        return result


def is_quiet_hours(
    when: dt.datetime | None = None,
    *,
    timezone: str = BUTLER_TIMEZONE,
) -> bool:
    """Return whether Butler delivery is inside the 22:00–08:00 quiet window."""
    zone = ZoneInfo(timezone)
    current = when or dt.datetime.now(dt.timezone.utc)
    if current.tzinfo is None:
        raise ValidationError("quiet-hours timestamp must be timezone-aware")
    hour = current.astimezone(zone).hour
    return hour >= 22 or hour < 8


def _delivery_response(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    raw = (result.stdout or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"output": raw[-4000:]}
    return parsed if isinstance(parsed, dict) else {"output": parsed}


def covered_research(days: int = 14, *, runs_root: Path | None = None, now: dt.datetime | None = None) -> list[str]:
    """Vault-relative research report paths already covered by rendered runs.

    Scans completed runs' episode.json newer than the window and collects
    deep-research inputs from coverage.inputs_checked and vault:// research
    sources, so twice-daily episodes never repeat a report.
    """
    root = (runs_root or RUNS_ROOT).expanduser().resolve()
    cutoff = (now or dt.datetime.now(dt.timezone.utc)) - dt.timedelta(days=max(1, int(days)))
    covered: set[str] = set()
    if not root.is_dir():
        return []
    for run_dir in root.iterdir():
        episode_path = run_dir / "episode.json"
        if not run_dir.is_dir() or not episode_path.is_file():
            continue
        try:
            modified = dt.datetime.fromtimestamp(episode_path.stat().st_mtime, tz=dt.timezone.utc)
            if modified < cutoff:
                continue
            episode = json.loads(episode_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(episode, dict):
            continue
        coverage = episode.get("coverage") or {}
        if isinstance(coverage, dict):
            for entry in coverage.get("inputs_checked") or []:
                text = str(entry).strip()
                if text.startswith("Research/"):
                    covered.add(text.split(" (")[0].strip())
        for source in episode.get("sources") or []:
            if not isinstance(source, dict):
                continue
            url = str(source.get("url", ""))
            if url.startswith("vault://Research/"):
                covered.add(url.removeprefix("vault://"))
    return sorted(covered)


def publish_run(
    run_dir: str | Path,
    *,
    approval: str | None,
    account: str = "butler",
    target: str = "253483520",
    summary: str | None = None,
    dry_run: bool = False,
    allow_quiet_hours: bool = False,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    """Idempotently send a completed run and its credits to Boris on Telegram.

    Publication is deliberately separate from rendering. Only artifacts under
    the canonical run root can be sent, and a durable partial receipt prevents
    re-sending the MP4 if the credits upload needs a retry.
    """
    resolved = Path(run_dir).expanduser().resolve()
    runs_root = RUNS_ROOT.expanduser().resolve()
    try:
        resolved.relative_to(runs_root)
    except ValueError as exc:
        raise ValidationError(f"publish run must be inside {runs_root}: {resolved}") from exc

    success = resolved / "SUCCESS"
    run_json = resolved / "run.json"
    episode_json = resolved / "episode.json"
    video = resolved / "render" / "episode.mp4"
    credits = resolved / "credits.md"
    for required in (success, run_json, episode_json, video, credits):
        if not required.is_file():
            raise RenderError(f"completed publish artifact is missing: {required}")
        if required.is_symlink():
            raise ValidationError(f"publish artifacts must not be symbolic links: {required}")

    episode, _ = load_episode(episode_json)
    validate_episode(episode, episode_path=episode_json, strict=True)
    render_meta = json.loads(run_json.read_text(encoding="utf-8"))
    if render_meta.get("episode_id") != episode.get("episode_id"):
        raise ValidationError("run.json episode_id does not match episode.json")
    if render_meta.get("episode_sha256") != _episode_digest(episode):
        raise ValidationError("episode.json checksum does not match run.json")
    actual_sha = hashlib.sha256(video.read_bytes()).hexdigest()
    if render_meta.get("sha256") != actual_sha:
        raise ValidationError("rendered video checksum does not match run.json")

    receipt_path = resolved / "publish" / "telegram.json"
    if receipt_path.is_file():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt.get("status") == "published":
            return receipt
    else:
        receipt = {
            "schema_version": "butler.news-video-publish/v1",
            "status": "pending",
            "episode_id": episode["episode_id"],
            "channel": "telegram",
            "account": account,
            "target": target,
        }

    account = _safe_id(account, "account")
    if not re.fullmatch(r"-?[0-9]{5,20}", str(target)):
        raise ValidationError("Telegram target must be a numeric chat id")
    if receipt.get("account") != account or str(receipt.get("target")) != str(target):
        raise ValidationError("publish retry account/target does not match the existing receipt")
    approval_text = str(approval or "").strip()
    if not dry_run and (not approval_text or len(approval_text) > 200 or _has_control_chars(approval_text)):
        raise DeliveryApprovalRequired(
            "Telegram delivery requires --approve-delivery with the user request or authorized schedule reference"
        )
    quiet_now = is_quiet_hours(now)
    if not dry_run and quiet_now and not allow_quiet_hours:
        raise DeliveryApprovalRequired(
            f"delivery is blocked during quiet hours (22:00–08:00 {BUTLER_TIMEZONE}); "
            "--allow-quiet-hours requires Boris's standing authorization (e.g. the 05:00 schedule)"
        )

    factual_scenes = sum(1 for scene in episode.get("scenes", []) if scene.get("kind") in FACTUAL_SCENE_KINDS)
    source_count = len(episode.get("sources", []))
    message_summary = str(summary or episode.get("title") or "AI Butler — видеобрифинг").strip()
    if _has_control_chars(message_summary):
        raise ValidationError("publish summary contains forbidden control characters")
    if len(message_summary) > 500:
        message_summary = message_summary[:497].rstrip() + "…"
    video_message = (
        f"🎬 {message_summary}\n"
        f"{factual_scenes} сюжетов · {source_count} источников · episode {episode['episode_id']}\n"
        "Озвучка создана системным/AI-синтезатором. Источники и ограничения — следующим файлом."
    )
    credits_message = f"📎 Источники и ограничения · {episode['episode_id']}"
    staging_dir = OPENCLAW_MEDIA_STAGING / episode["episode_id"]
    staged_video = staging_dir / "episode.mp4"
    staged_credits = staging_dir / "credits.md"
    if not dry_run:
        staging_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(video, staged_video)
        shutil.copy2(credits, staged_credits)
        if hashlib.sha256(staged_video.read_bytes()).hexdigest() != actual_sha:
            raise RenderError("staged video checksum does not match the verified render")
    video_command = [
        OPENCLAW,
        "message",
        "send",
        "--channel",
        "telegram",
        "--account",
        account,
        "--target",
        str(target),
        "--media",
        str(staged_video),
        "--message",
        video_message,
        "--json",
    ]
    credits_command = [
        OPENCLAW,
        "message",
        "send",
        "--channel",
        "telegram",
        "--account",
        account,
        "--target",
        str(target),
        "--media",
        str(staged_credits),
        "--force-document",
        "--message",
        credits_message,
        "--json",
    ]
    if dry_run:
        return {
            "status": "dry-run-no-delivery",
            "episode_id": episode["episode_id"],
            "video": str(video),
            "credits": str(credits),
            "video_message": video_message,
            "credits_message": credits_message,
        }

    receipt.update({"approval_reference": approval_text, "updated_at": _now_iso()})
    if quiet_now and allow_quiet_hours:
        receipt["quiet_hours_bypassed"] = True
    if receipt.get("status") not in {"video-sent", "published"}:
        video_result = _run(video_command, timeout=180)
        receipt.update(
            {
                "status": "video-sent",
                "video_sent_at": _now_iso(),
                "video_response": _delivery_response(video_result),
                "updated_at": _now_iso(),
            }
        )
        _atomic_write_json(receipt_path, receipt)

    credits_result = _run(credits_command, timeout=180)
    receipt.update(
        {
            "status": "published",
            "credits_sent_at": _now_iso(),
            "credits_response": _delivery_response(credits_result),
            "updated_at": _now_iso(),
        }
    )
    _atomic_write_json(receipt_path, receipt)
    shutil.rmtree(staging_dir, ignore_errors=True)
    return receipt


def veo_plan(
    episode: dict[str, Any],
    *,
    model: str = "veo-3.1-fast-generate-preview",
    cost_per_second: float | None = None,
) -> list[VeoRequest]:
    if model not in VEO_COST_PER_SECOND:
        raise ValidationError(f"unsupported/unpriced Veo model: {model}")
    override: float | None = None
    if cost_per_second is not None:
        try:
            override = float(cost_per_second)
        except (TypeError, ValueError) as exc:
            raise ValidationError("Veo cost override must be a finite positive number") from exc
        if not math.isfinite(override) or override <= 0:
            raise ValidationError("Veo cost override must be a finite positive number")
    requests: list[VeoRequest] = []
    veo_width, veo_height, _veo_fps = episode_format(episode)
    aspect = "9:16" if veo_height >= veo_width else "16:9"
    for scene in episode.get("scenes", []):
        visual = scene.get("visual") or {}
        if visual.get("mode") != "veo":
            continue
        if scene.get("kind") not in {"intro", "outro"}:
            raise ValidationError(f"Veo is only allowed for intro/outro: {scene.get('id')}")
        duration = int(visual.get("veo_duration", 4))
        if duration not in {4, 6, 8}:
            raise ValidationError(f"scene {scene['id']}: veo_duration must be 4, 6, or 8")
        resolution = str(visual.get("veo_resolution", "720p"))
        published_rate = VEO_COST_PER_SECOND[model].get(resolution)
        if published_rate is None:
            raise ValidationError(f"model {model} does not support priced resolution {resolution}")
        if resolution in {"1080p", "4k"} and duration != 8:
            raise ValidationError(f"scene {scene['id']}: {resolution} output requires veo_duration=8")
        if override is not None and override + 1e-9 < published_rate:
            raise ValidationError(
                f"Veo cost override ${override:.2f}/s is below the published {resolution} rate "
                f"${published_rate:.2f}/s"
            )
        rate = override if override is not None else published_rate
        prompt = str(visual.get("veo_prompt", "")).strip()
        if not prompt:
            raise ValidationError(f"scene {scene['id']}: missing veo_prompt")
        requests.append(
            VeoRequest(
                scene_id=scene["id"],
                prompt=prompt,
                duration_seconds=duration,
                model=model,
                aspect_ratio=aspect,
                resolution=resolution,
                estimated_cost_usd=round(duration * rate, 2),
            )
        )
    return requests


def generate_veo_assets(
    episode_path: str | Path,
    *,
    output_dir: str | Path | None = None,
    model: str = "veo-3.1-fast-generate-preview",
    cost_per_second: float | None = None,
    max_usd: float | None = None,
    paid_api_approval: str | None = None,
    timeout_seconds: int = 900,
) -> dict[str, Any]:
    """Generate decorative intro/outro clips through Gemini SDK.

    This function is never called by normal rendering. It requires both an
    explicit approval reference and a max budget large enough for the estimate.
    """
    episode, resolved_episode_path = load_episode(episode_path)
    validate_episode(episode, episode_path=resolved_episode_path, strict=True)
    requests = veo_plan(episode, model=model, cost_per_second=cost_per_second)
    estimate = round(sum(request.estimated_cost_usd for request in requests), 2)
    if not requests:
        return {"status": "nothing-to-generate", "estimated_cost_usd": 0.0, "assets": []}
    approval_reference, _ = _paid_budget_authorized(
        stage="Veo",
        approval=paid_api_approval,
        max_usd=max_usd,
        estimate_usd=estimate,
    )
    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        raise PaidAPIApprovalRequired("GEMINI_API_KEY/GOOGLE_API_KEY is not set; no Veo call was made")

    from google import genai
    from google.genai import types

    run_dir = Path(output_dir).expanduser().resolve() if output_dir else (RUNS_ROOT / episode["episode_id"]).resolve()
    with _locked_run_dir(run_dir):
        if run_dir.exists() and any(run_dir.iterdir()):
            _assert_run_identity(run_dir, episode["episode_id"], _episode_digest(episode))
            publish_receipt = run_dir / "publish" / "telegram.json"
            if publish_receipt.is_file():
                receipt = _read_json_object(publish_receipt, "publish receipt")
                if receipt.get("status") == "published":
                    raise RenderError(f"refusing to mutate a published run: {run_dir}")

        assets_dir = run_dir / "assets" / "veo"
        assets_dir.mkdir(parents=True, exist_ok=True)
        success_file = run_dir / "SUCCESS"
        if success_file.is_file():
            success_file.unlink()
        client = genai.Client()
        assets: list[dict[str, Any]] = []
        deadline = time.monotonic() + timeout_seconds
        for request in requests:
            out_path = assets_dir / f"{request.scene_id}.mp4"
            if out_path.is_file() and _duration(out_path) > 0.5:
                assets.append({"scene_id": request.scene_id, "path": str(out_path), "cached": True})
                continue
            operation = client.models.generate_videos(
                model=request.model,
                prompt=request.prompt,
                config=types.GenerateVideosConfig(
                    number_of_videos=1,
                    duration_seconds=request.duration_seconds,
                    aspect_ratio=request.aspect_ratio,
                    resolution=request.resolution,
                ),
            )
            while not operation.done:
                if time.monotonic() >= deadline:
                    raise RenderError(
                        f"Veo timed out after {timeout_seconds}s; renderer can still use template fallback"
                    )
                time.sleep(10)
                operation = client.operations.get(operation)
            if operation.error:
                raise RenderError(f"Veo rejected scene {request.scene_id}: {operation.error}")
            response = operation.response
            generated = list(response.generated_videos or []) if response else []
            if not generated:
                reasons = list(response.rai_media_filtered_reasons or []) if response else []
                raise RenderError(f"Veo returned no clip for {request.scene_id}; filters={reasons}")
            data = client.files.download(file=generated[0].video)
            out_path.write_bytes(data)
            info = probe_media(out_path)
            if not any(stream.get("codec_type") == "video" for stream in info.get("streams", [])):
                out_path.unlink(missing_ok=True)
                raise RenderError(f"Veo asset for {request.scene_id} has no video stream")
            assets.append(
                {
                    "scene_id": request.scene_id,
                    "path": str(out_path),
                    "cached": False,
                    "estimated_cost_usd": request.estimated_cost_usd,
                }
            )
        result = {
            "status": "generated",
            "model": model,
            "estimated_cost_usd": estimate,
            "approval_reference": approval_reference,
            "assets": assets,
        }
        (run_dir / "veo.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        return result


def make_demo_episode(profile: str = "daily", *, compact: bool = False) -> dict[str, Any]:
    if not isinstance(profile, str) or profile not in PROFILE_RULES:
        raise ValidationError(f"unknown profile: {profile}")
    now = dt.datetime.now(dt.timezone.utc).astimezone().replace(microsecond=0)
    start = now - dt.timedelta(hours=24)
    duration = 1.2 if compact else 3.5
    _demo_format = PROFILE_RULES[profile].get("format") or DEFAULT_FORMAT
    _demo_landscape = _demo_format["width"] > _demo_format["height"]
    episode = {
        "schema_version": SCHEMA_VERSION,
        "profile": profile,
        "title": "AI Butler — тестовый выпуск",
        "subtitle": "Графика · источники · голос",
        "language": "ru-RU",
        "as_of": now.isoformat(),
        "window": {"from": start.isoformat(), "to": now.isoformat(), "timezone": "America/Chicago"},
        # Demo frames follow the profile's own orientation, shrunk when compact.
        "format": (
            {
                "width": 640 if _demo_landscape else 360,
                "height": 360 if _demo_landscape else 640,
                "fps": 12,
            }
            if compact
            else dict(PROFILE_RULES[profile].get("format") or DEFAULT_FORMAT)
        ),
        "coverage": {
            "scope": ["техническая демонстрация"],
            "inputs_checked": ["synthetic-fixture"],
            "limitations": ["Это демонстрационный выпуск без реальных новостей."],
        },
        "sources": [
            {
                "id": "src-demo",
                "title": "Example Domain",
                "publisher": "IANA",
                "url": "https://example.com",
                "trust_tier": "primary",
                "retrieved_at": now.isoformat(),
            }
        ],
        "scenes": [
            {
                "id": "intro",
                "kind": "intro",
                "kicker": "Утренний брифинг",
                "headline": "Новости, которые имеют значение",
                "body": "Коротко, проверяемо и с полным списком источников.",
                "narration": "Доброе утро.[pause:0.35] Это тестовый выпуск новостного видео AI Butler.[breath] Здесь голос, паузы и смысловые визуалы работают вместе.",
                "source_ids": [],
                "status": "not-applicable",
                "duration_hint": duration,
                "visual": {
                    "mode": "veo",
                    "accent": "#5DE1FF",
                    "subject": "Morning briefing",
                    "brief": "Calm live-news opening with soft motion and clean data cues.",
                    "veo_prompt": "Abstract luminous data streams forming a calm morning news interface, elegant dark blue palette, no text, vertical composition",
                    "veo_duration": 4,
                },
            },
            {
                "id": "story-demo",
                "kind": "story",
                "kicker": "Система готова",
                "headline": "Факты отделены от оформления",
                "body": "Каждый новостной блок связан с источником; графика и Veo не считаются доказательством.",
                "narration": "Каждый фактический блок обязан ссылаться на источник.[pause:0.28] Генеративная графика используется только как оформление.",
                "source_ids": ["src-demo"],
                "status": "confirmed",
                "duration_hint": duration,
                "visual": {
                    "mode": "template",
                    "accent": "#A78BFA",
                    "subject": "Verified source chain",
                    "brief": "One fact, one source chain, one visible claim.",
                    "points": [
                        "Каждый факт связан с источником",
                        "Графика — оформление, не доказательство",
                        "Статус виден прямо на карточке",
                    ],
                    "stat": {"value": "1:1", "label": "каждый факт — минимум один источник"},
                },
            },
            {
                "id": "outro",
                "kind": "outro",
                "kicker": "Дальше",
                "headline": "Полный дайджест — рядом с видео",
                "body": "В Telegram прикладываются ссылки, ограничения покрытия и текст выпуска.",
                "narration": "Полный список источников и ограничений всегда прикладывается к ролику.[breath] Это не финальная версия новости, а проверяемый видеобрифинг.",
                "source_ids": [],
                "status": "not-applicable",
                "duration_hint": duration,
                "visual": {
                    "mode": "veo",
                    "accent": "#34D399",
                    "subject": "Closing checkmark",
                    "brief": "A neat ending that signals completion without pretending to show evidence.",
                    "veo_prompt": "Minimal glowing horizon resolving into a clean check mark made of light, dark navy background, premium technology brand outro, no text, vertical composition",
                    "veo_duration": 4,
                },
            },
        ],
        "provenance": {
            "research_provider": "synthetic-demo",
            "renderer_version": SCHEMA_VERSION,
            "generated_at": _now_iso(),
        },
    }
    episode["episode_id"] = deterministic_episode_id(episode)
    return episode


def doctor() -> dict[str, Any]:
    checks = {
        "python": {"path": PYTHON, "ok": Path(PYTHON).is_file()},
        "ffmpeg": {"path": FFMPEG, "ok": Path(FFMPEG).is_file()},
        "ffprobe": {"path": FFPROBE, "ok": Path(FFPROBE).is_file()},
        "say": {"path": SAY, "ok": Path(SAY).is_file()},
        "font_regular": {"path": FONT_REGULAR, "ok": Path(FONT_REGULAR).is_file()},
        "font_bold": {"path": FONT_BOLD, "ok": Path(FONT_BOLD).is_file()},
        "openai_tts_cli": {"path": OPENAI_TTS_CLI, "ok": Path(OPENAI_TTS_CLI).is_file()},
        "edge_tts": {"ok": False, "default_voice": EDGE_TTS_DEFAULT_VOICE},
        "gemini_sdk": {"ok": False},
    }
    try:
        import edge_tts  # noqa: F401

        checks["edge_tts"]["ok"] = True
    except Exception as exc:  # pragma: no cover - environment dependent
        checks["edge_tts"]["error"] = type(exc).__name__
        checks["edge_tts"]["note"] = "free neural narration falls back to macOS Milena"
    try:
        from google import genai  # noqa: F401

        checks["gemini_sdk"]["ok"] = True
    except Exception as exc:  # pragma: no cover - environment dependent
        checks["gemini_sdk"]["error"] = type(exc).__name__
    return {
        "ok": all(item.get("ok") for item in checks.values()),
        "checks": checks,
        "paid_api_credentials": {
            "gemini_configured": bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")),
            "openai_configured": bool(os.environ.get("OPENAI_API_KEY")),
            "note": "Only presence is reported; values are never read or logged.",
        },
    }
