"""Policy gate and deployment wrapper for the isolated Bamberg News service.

The renderer is shared with Butler's existing news-video pipeline, but this
module deliberately keeps Bamberg inputs, run files, YouTube credentials and
the editorial policy separate.  It is a guardrail, not a news crawler: the
scheduled editor still researches current sources, and this module rejects an
episode that does not show how its factual claims were confirmed.
"""
from __future__ import annotations

import contextlib
import datetime as dt
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from lib import news_video


EDITORIAL_PROFILE = "bamberg-news/v1"
TOPICS = ("local", "germany", "laws", "europe", "world", "ukraine-russia", "medicine", "pension-life", "city-life", "science", "weather", "health", "garden")
ALLOWED_COVERAGE_STATUSES = {"covered", "no_verified_update"}
FACTUAL_KINDS = {"story", "finding"}
ALLOWED_EVIDENCE_METHODS = {"primary", "multi-source"}

DEFAULT_WORKSPACE = Path("/Users/boris/.openclaw/workspace-bamberg-news")
DEFAULT_RUNS_ROOT = DEFAULT_WORKSPACE / "runs" / "news-video"
DEFAULT_MOTION_CACHE = DEFAULT_WORKSPACE / "assets" / "motion-cache"
DEFAULT_YOUTUBE_CREDENTIALS = DEFAULT_WORKSPACE / "credentials" / "youtube"
DEFAULT_YOUTUBE_CLI = Path("/Users/boris/.openclaw/workspace-butler/bin/butler-youtube")
DEFAULT_DESTINATION_FILE = DEFAULT_WORKSPACE / "state" / "youtube-destination.json"


class BambergNewsError(news_video.NewsVideoError):
    """Base error for the separate Bamberg News service."""


class EditorialPolicyError(BambergNewsError):
    """The episode is structurally valid but does not satisfy Bamberg policy."""


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def runs_root() -> Path:
    return Path(os.environ.get("BAMBERG_NEWS_RUNS", DEFAULT_RUNS_ROOT)).expanduser().resolve()


def youtube_credentials_dir() -> Path:
    return Path(
        os.environ.get("BAMBERG_NEWS_YOUTUBE_CREDENTIALS", DEFAULT_YOUTUBE_CREDENTIALS)
    ).expanduser().resolve()


def youtube_cli() -> Path:
    return Path(os.environ.get("BAMBERG_NEWS_YOUTUBE_CLI", DEFAULT_YOUTUBE_CLI)).expanduser().resolve()


def destination_file() -> Path:
    return Path(
        os.environ.get("BAMBERG_NEWS_YOUTUBE_DESTINATION", DEFAULT_DESTINATION_FILE)
    ).expanduser().resolve()


def configure_youtube_destination(channel_id: str) -> dict[str, Any]:
    """Persist a non-secret channel allowlist for scheduled uploads."""
    channel = str(channel_id or "").strip()
    if not channel.startswith("UC") or not channel.replace("-", "").replace("_", "").isalnum() or not (10 <= len(channel) <= 128):
        raise EditorialPolicyError("YouTube channel ID must start with UC and contain only URL-safe ID characters")
    path = destination_file()
    news_video._atomic_write_json(
        path,
        {
            "schema_version": "bamberg-news-youtube-destination/v1",
            "channel_id": channel,
            "configured_at": news_video._now_iso(),
        },
    )
    return {"status": "configured", "channel_id": channel, "destination_file": str(path)}


def configured_youtube_channel_id() -> str | None:
    explicit = os.environ.get("BAMBERG_NEWS_YOUTUBE_CHANNEL_ID", "").strip()
    if explicit:
        return explicit
    path = destination_file()
    if not path.is_file():
        return None
    try:
        configured = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BambergNewsError(f"invalid YouTube destination file: {path}") from exc
    if configured.get("schema_version") != "bamberg-news-youtube-destination/v1":
        raise BambergNewsError(f"unsupported YouTube destination file: {path}")
    channel = str(configured.get("channel_id", "")).strip()
    return channel or None


def _parse_iso(value: Any, field: str, errors: list[str]) -> None:
    text = str(value).strip()
    try:
        # Many official German notices expose a reliable publication *date* but
        # not a clock time. Preserve that precision instead of inventing one.
        if len(text) == 10:
            dt.date.fromisoformat(text)
            return
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise ValueError
    except (TypeError, ValueError):
        errors.append(f"{field} must be an ISO date or timezone-aware ISO timestamp")


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower().rstrip(".")


def _domain_matches(host: str, domain: str) -> bool:
    return host == domain or host.endswith(f".{domain}")


def load_source_catalog(path: str | Path | None = None) -> dict[str, Any]:
    catalog_path = Path(path) if path else project_root() / "sources.json"
    try:
        catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BambergNewsError(f"source catalog not found: {catalog_path}") from exc
    except json.JSONDecodeError as exc:
        raise BambergNewsError(f"invalid source catalog at {catalog_path}: {exc}") from exc
    if catalog.get("schema_version") != "bamberg-news-sources/v1":
        raise BambergNewsError(f"unsupported source catalog: {catalog_path}")
    if not isinstance(catalog.get("sources"), list):
        raise BambergNewsError("source catalog sources must be an array")
    return catalog


def _registered_source(url: str, catalog: dict[str, Any]) -> dict[str, Any] | None:
    host = _host(url)
    if not host:
        return None
    for source in catalog["sources"]:
        if any(_domain_matches(host, str(domain).lower()) for domain in source.get("domains", [])):
            return source
    return None


def _source_for_id(sources: list[dict[str, Any]], source_id: str) -> dict[str, Any] | None:
    return next((source for source in sources if source.get("id") == source_id), None)


def _visual_format_errors(episode: dict[str, Any]) -> list[str]:
    """Bamberg is watched on a television (Boris 2026-08-01).

    Three rules follow from that and are enforced here, not left to the
    worker's goodwill: the frame is landscape, a map is shown whole, and a
    factual photo scene changes its picture at least once.
    """
    errors: list[str] = []
    fmt = episode.get("format")
    if isinstance(fmt, dict) and fmt:
        try:
            width, height = int(fmt.get("width", 0)), int(fmt.get("height", 0))
        except (TypeError, ValueError):
            width = height = 0
        if width and height and width <= height:
            errors.append(
                f"Bamberg News is a 16:9 edition: format must be landscape, got {width}x{height}"
            )
    for scene in episode.get("scenes", []):
        if not isinstance(scene, dict):
            continue
        visual = scene.get("visual")
        if not isinstance(visual, dict) or visual.get("mode") != "broll":
            continue
        scene_id = scene.get("id", "<unknown>")
        paths = visual.get("broll_paths")
        if not isinstance(paths, list) or not paths:
            paths = [visual["broll_path"]] if visual.get("broll_path") else []
        paths = [str(item) for item in paths]
        is_map = any("/maps/" in item or item.startswith("maps/") for item in paths)
        if is_map and visual.get("fit") != "contain":
            errors.append(
                f"scene {scene_id}: a map must set visual.fit=\"contain\" so it is shown whole"
            )
        if not is_map and scene.get("kind") in FACTUAL_KINDS and len(paths) < 2:
            errors.append(
                f"scene {scene_id}: a factual photo scene needs 2-4 broll_paths so the picture changes"
            )
    return errors


def validate_bamberg_episode(
    episode: dict[str, Any],
    *,
    catalog_path: str | Path | None = None,
) -> list[str]:
    """Validate the Bamberg-specific evidence and coverage contract.

    ``news_video.validate_episode`` remains the generic schema/evidence check.
    This adds category coverage, registered-source tiers, a stricter Ukraine
    rule, separate daily DWD requirements for the local weather and garden
    sections, and a health-safety guardrail. Weather and health are
    intentionally first-class sections for the 70+ audience and must never be
    omitted as side notes.
    """
    catalog = load_source_catalog(catalog_path)
    errors: list[str] = []
    warnings: list[str] = []

    if episode.get("editorial_profile") != EDITORIAL_PROFILE:
        errors.append(f"editorial_profile must be {EDITORIAL_PROFILE!r}")
    if episode.get("profile") not in ("daily", "bamberg-daily"):
        errors.append("Bamberg News supports profiles daily and bamberg-daily only")
    if not str(episode.get("language", "")).lower().startswith("ru"):
        errors.append("Bamberg News narration must be in Russian (language=ru)")
    errors.extend(_visual_format_errors(episode))

    coverage = episode.get("coverage")
    category_status: dict[str, Any] = {}
    if not isinstance(coverage, dict):
        errors.append("coverage must be an object")
    else:
        category_status = coverage.get("category_status")
        if not isinstance(category_status, dict):
            errors.append("coverage.category_status must be an object")
            category_status = {}
        for topic in TOPICS:
            status = category_status.get(topic)
            if not isinstance(status, dict):
                errors.append(f"coverage.category_status.{topic} must be an object")
                continue
            if status.get("status") not in ALLOWED_COVERAGE_STATUSES:
                errors.append(
                    f"coverage.category_status.{topic}.status must be one of "
                    f"{sorted(ALLOWED_COVERAGE_STATUSES)}"
                )
            checked = status.get("sources_checked")
            if not isinstance(checked, list) or not checked:
                errors.append(f"coverage.category_status.{topic}.sources_checked must be non-empty")
        if category_status.get("weather", {}).get("status") != "covered":
            errors.append(
                "the weather section must be covered every run (Bamberg 24–48 hour forecast + DWD warning check)"
            )
        # Boris 2026-07-20: sections with no verified update are SKIPPED, not
        # padded. Only the safety-critical weather section is mandatory.

    raw_sources = episode.get("sources")
    sources = [source for source in raw_sources if isinstance(source, dict)] if isinstance(raw_sources, list) else []
    for source in sources:
        source_id = str(source.get("id", "<unknown>"))
        url = str(source.get("url", "")).strip()
        if not url.startswith("https://"):
            errors.append(f"source {source_id} must include an HTTPS article URL")
            continue
        for field in ("publisher", "published_at", "retrieved_at", "trust_tier"):
            if not str(source.get(field, "")).strip():
                errors.append(f"source {source_id} is missing {field}")
        _parse_iso(source.get("published_at"), f"source {source_id}.published_at", errors)
        _parse_iso(source.get("retrieved_at"), f"source {source_id}.retrieved_at", errors)
        registered = _registered_source(url, catalog)
        tier = str(source.get("trust_tier", ""))
        if registered and tier != registered["trust_tier"]:
            errors.append(
                f"source {source_id} declares {tier!r}, but {registered['name']} is "
                f"registered as {registered['trust_tier']!r}"
            )
        if not registered and tier == "primary":
            errors.append(
                f"source {source_id} is not in the source catalog and cannot self-declare primary"
            )
        if not registered:
            warnings.append(
                f"source {source_id} is an unregistered secondary source; show it only in multi-source evidence"
            )

    raw_scenes = episode.get("scenes")
    scenes = [scene for scene in raw_scenes if isinstance(scene, dict)] if isinstance(raw_scenes, list) else []
    topic_scene_counts = {topic: 0 for topic in TOPICS}
    for scene in scenes:
        scene_id = str(scene.get("id", "<unknown>"))
        kind = scene.get("kind")
        editorial = scene.get("editorial")
        if not isinstance(editorial, dict):
            errors.append(f"scene {scene_id} must contain an editorial object")
            continue
        topic = editorial.get("topic")
        if kind in news_video.FACTUAL_SCENE_KINDS:
            if topic not in TOPICS:
                errors.append(f"scene {scene_id} factual editorial.topic must be one of {list(TOPICS)}")
                continue
            topic_scene_counts[topic] += 1
            if editorial.get("fact_only") is not True:
                errors.append(f"scene {scene_id} must set editorial.fact_only=true")
            evidence = editorial.get("evidence")
            if not isinstance(evidence, dict):
                errors.append(f"scene {scene_id} must contain editorial.evidence")
                continue
            method = evidence.get("method")
            if method not in ALLOWED_EVIDENCE_METHODS:
                errors.append(
                    f"scene {scene_id} evidence.method must be one of {sorted(ALLOWED_EVIDENCE_METHODS)}"
                )
                continue
            refs = scene.get("source_ids") if isinstance(scene.get("source_ids"), list) else []
            referenced = [source for source_id in refs if (source := _source_for_id(sources, str(source_id)))]
            registered = [_registered_source(str(source.get("url", "")), catalog) for source in referenced]
            if method == "primary":
                if not any(entry and entry["trust_tier"] == "primary" for entry in registered):
                    errors.append(f"scene {scene_id} primary evidence needs a catalogued primary source")
            if method == "multi-source":
                publishers = {str(source.get("publisher", "")).strip().lower() for source in referenced}
                hosts = {_host(str(source.get("url", ""))) for source in referenced}
                if len(referenced) < 2 or len(publishers - {""}) < 2 or len(hosts - {""}) < 2:
                    errors.append(
                        f"scene {scene_id} multi-source evidence needs two independent publishers and domains"
                    )
            if topic == "ukraine-russia" and method != "multi-source":
                errors.append(
                    f"scene {scene_id} Ukraine/Russia claim must use multi-source confirmation"
                )
            if topic in {"weather", "garden"}:
                has_dwd = any(
                    entry and any(domain == "dwd.de" for domain in entry.get("domains", []))
                    for entry in registered
                )
                if not has_dwd:
                    errors.append(f"scene {scene_id} {topic} section must cite the Deutscher Wetterdienst")
            if topic == "weather":
                weather = editorial.get("weather")
                if not isinstance(weather, dict):
                    errors.append(f"scene {scene_id} weather section must contain editorial.weather")
                else:
                    if weather.get("area") != "Bamberg":
                        errors.append(f"scene {scene_id} weather.area must be Bamberg")
                    horizon = weather.get("horizon_hours")
                    if not isinstance(horizon, int) or not 24 <= horizon <= 48:
                        errors.append(
                            f"scene {scene_id} weather.horizon_hours must be an integer from 24 to 48"
                        )
                    for field in ("temperature", "conditions"):
                        if not str(weather.get(field, "")).strip():
                            errors.append(f"scene {scene_id} weather.{field} must be non-empty")
                    if weather.get("warnings_checked") is not True:
                        errors.append(f"scene {scene_id} weather.warnings_checked must be true")
            if topic == "health":
                has_health_authority = any(
                    entry
                    and entry.get("trust_tier") == "primary"
                    and "health" in entry.get("topics", [])
                    for entry in registered
                )
                if not has_health_authority:
                    errors.append(
                        f"scene {scene_id} health section must cite a catalogued primary German health authority"
                    )
                health = editorial.get("health")
                if not isinstance(health, dict):
                    errors.append(f"scene {scene_id} health section must contain editorial.health")
                else:
                    if health.get("guidance_type") not in {"prevention", "preparedness", "service"}:
                        errors.append(
                            f"scene {scene_id} health.guidance_type must be prevention, preparedness or service"
                        )
                    if health.get("individualised") is not False:
                        errors.append(f"scene {scene_id} health.individualised must be false")
                    if health.get("medication_advice") is not False:
                        errors.append(f"scene {scene_id} health.medication_advice must be false")
            if topic == "garden":
                if editorial.get("garden_advice") is True:
                    has_garden_authority = any(
                        entry
                        and entry.get("trust_tier") == "primary"
                        and "garden" in entry.get("topics", [])
                        and not any(domain == "dwd.de" for domain in entry.get("domains", []))
                        for entry in registered
                    )
                    if not has_garden_authority:
                        errors.append(
                            f"scene {scene_id} garden advice must cite a primary Bavarian/local gardening authority"
                        )
        elif topic not in {"intro", "outro", "context", None}:
            errors.append(f"scene {scene_id} non-factual editorial.topic is invalid: {topic!r}")

    for topic, count in topic_scene_counts.items():
        status = category_status.get(topic, {}).get("status")
        if status == "covered" and count == 0:
            errors.append(f"coverage says {topic} is covered, but it has no factual scene")
        if status == "no_verified_update" and count:
            errors.append(f"coverage says {topic} has no verified update, but it has a factual scene")

    if errors:
        raise EditorialPolicyError("\n".join(f"- {error}" for error in errors))
    return warnings


@contextlib.contextmanager
def _bamberg_renderer_roots() -> Iterator[None]:
    """Temporarily isolate shared renderer global paths for this process."""
    old_runs = news_video.RUNS_ROOT
    old_motion = news_video.MOTION_CACHE_DIR
    news_video.RUNS_ROOT = runs_root()
    news_video.MOTION_CACHE_DIR = Path(
        os.environ.get("BAMBERG_NEWS_MOTION_CACHE", DEFAULT_MOTION_CACHE)
    ).expanduser().resolve()
    try:
        yield
    finally:
        news_video.RUNS_ROOT = old_runs
        news_video.MOTION_CACHE_DIR = old_motion


def validate_episode_file(path: str | Path) -> dict[str, Any]:
    episode, episode_path = news_video.load_episode(path)
    generic_warnings = news_video.validate_episode(episode, episode_path=episode_path, strict=True)
    policy_warnings = validate_bamberg_episode(episode)
    return {
        "status": "valid",
        "episode_id": episode["episode_id"],
        "warnings": generic_warnings + policy_warnings,
    }


def render_episode(
    path: str | Path,
    *,
    output_dir: str | Path | None = None,
    tts: str = "edge",
    voice: str | None = None,
    rate: int = 175,
    approve_paid_api: str | None = None,
    max_usd: float | None = None,
) -> dict[str, Any]:
    if tts not in {"edge", "macos", "none", "openai"}:
        raise BambergNewsError("Bamberg News permits openai/edge/macos narration or caption-only output")
    if tts == "openai" and not approve_paid_api:
        # Boris's standing authorization (2026-07-19, «мы теперь используем
        # openai для tts») still requires the explicit reference on each call.
        raise BambergNewsError("openai TTS requires --approve-paid-api and --max-usd")
    checked = validate_episode_file(path)
    episode, _ = news_video.load_episode(path)
    run_dir = Path(output_dir).expanduser().resolve() if output_dir else runs_root() / episode["episode_id"]
    try:
        run_dir.relative_to(runs_root())
    except ValueError as exc:
        raise EditorialPolicyError(f"Bamberg run must be inside {runs_root()}: {run_dir}") from exc
    selected_voice = voice or news_video.DEFAULT_TTS_VOICES.get(tts)
    with _bamberg_renderer_roots():
        result = news_video.render_episode(
            path,
            output_dir=run_dir,
            tts=tts,
            voice=selected_voice,
            rate=rate,
            overwrite=False,
            paid_api_approval=approve_paid_api,
            max_usd=max_usd,
        )
    result["editorial_profile"] = EDITORIAL_PROFILE
    result["validation_warnings"] = checked["warnings"]
    return result


def _youtube_env() -> dict[str, str]:
    env = os.environ.copy()
    env["BUTLER_NEWS_VIDEO_RUNS"] = str(runs_root())
    env["BUTLER_YOUTUBE_CREDENTIALS"] = str(youtube_credentials_dir())
    return env


def _run_youtube(args: list[str]) -> dict[str, Any]:
    cli = youtube_cli()
    if not cli.is_file():
        raise BambergNewsError(
            f"shared YouTube CLI not found: {cli}; install the verified Butler YouTube uploader first"
        )
    result = subprocess.run(
        [str(cli), *args],
        capture_output=True,
        text=True,
        env=_youtube_env(),
        check=False,
    )
    raw = (result.stdout or "").strip()
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        payload = {"output": raw[-4000:]}
    if result.returncode != 0:
        detail = (result.stderr or raw or "unknown uploader error").strip()[-1000:]
        raise BambergNewsError(f"YouTube uploader failed ({result.returncode}): {detail}")
    if not isinstance(payload, dict):
        raise BambergNewsError("YouTube uploader returned an invalid response")
    return payload


def youtube_status(*, expected_channel_id: str | None = None) -> dict[str, Any]:
    expected = expected_channel_id or configured_youtube_channel_id()
    if not expected:
        raise BambergNewsError(
            "configure the dedicated channel first: bamberg-news configure-youtube <UC...>"
        )
    report = _run_youtube(["status"])
    channel = report.get("channel") if isinstance(report.get("channel"), dict) else {}
    actual = str(channel.get("channel_id", ""))
    if not report.get("ready"):
        raise BambergNewsError(f"Bamberg YouTube credentials are not ready: {report.get('error') or report.get('hint')}")
    if actual != expected:
        raise BambergNewsError(
            f"refusing upload: authenticated channel {actual or '<none>'} does not match "
            f"BAMBERG_NEWS_YOUTUBE_CHANNEL_ID={expected}"
        )
    return {"ready": True, "channel": channel, "credentials_dir": str(youtube_credentials_dir())}


# YouTube silently truncates longer titles.
MAX_YOUTUBE_TITLE = 100

RU_MONTHS_GENITIVE = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)


def episode_title(episode_path: str | Path) -> str:
    """YouTube title: the episode headline plus its Bamberg date and time.

    Boris 2026-08-01: «в названии видео должна быть дата и время». The stamp is
    derived from `as_of` in the episode's own timezone, so a title can never
    drift from the content — and it is composed here, in code, rather than left
    to the worker to remember. Falls back to the plain title if `as_of` is
    unusable; a missing stamp must never block a publication.
    """
    episode, _ = news_video.load_episode(episode_path)
    base = str(episode.get("title") or episode.get("episode_id") or "Bamberg News").strip()
    window = episode.get("window") if isinstance(episode.get("window"), dict) else {}
    tz_name = str(window.get("timezone") or "Europe/Berlin")
    try:
        stamp = dt.datetime.fromisoformat(str(episode.get("as_of", "")).replace("Z", "+00:00"))
        local = stamp.astimezone(ZoneInfo(tz_name))
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        return base
    day, month = local.day, RU_MONTHS_GENITIVE[local.month - 1]
    suffix = f" — {day} {month} {local.year}, {local:%H:%M}"
    if len(base) + len(suffix) > MAX_YOUTUBE_TITLE:
        # Give up the year before the clock, then trim the headline — the
        # stamp is the point of this title and must always survive.
        suffix = f" — {day} {month}, {local:%H:%M}"
    if len(base) + len(suffix) > MAX_YOUTUBE_TITLE:
        base = base[: MAX_YOUTUBE_TITLE - len(suffix) - 1].rstrip() + "…"
    return base + suffix


def publish_to_youtube(
    run_dir: str | Path,
    *,
    approval: str | None,
    privacy: str = "public",
    dry_run: bool = False,
    expected_channel_id: str | None = None,
) -> dict[str, Any]:
    resolved = Path(run_dir).expanduser().resolve()
    try:
        resolved.relative_to(runs_root())
    except ValueError as exc:
        raise EditorialPolicyError(f"Bamberg run must be inside {runs_root()}: {resolved}") from exc
    episode_path = resolved / "episode.json"
    if not episode_path.is_file():
        raise BambergNewsError(f"episode.json not found in run: {resolved}")
    validate_episode_file(episode_path)
    if not dry_run:
        youtube_status(expected_channel_id=expected_channel_id)
    args = ["upload", str(resolved), "--privacy", privacy, "--title", episode_title(episode_path)]
    if approval:
        args += ["--approve-upload", approval]
    if dry_run:
        args.append("--dry-run")
    result = _run_youtube(args)
    result["editorial_profile"] = EDITORIAL_PROFILE
    return result
