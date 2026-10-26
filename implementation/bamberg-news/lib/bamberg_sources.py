"""Deterministic evidence collector for Bamberg News.

Fetches structured feeds (city press RSS, Google News aggregations, tagesschau)
and DWD weather via Bright Sky BEFORE the editorial agent runs, so the agent
edits a complete evidence pack instead of hoping ad-hoc web search finds
everything.

Adaptive sources (Boris 2026-07-20): the collector counts which domains
published episodes actually cite; domains cited often get automatic RSS
discovery and join the permanent fetch list, so a source we rely on is read in
full from then on.
"""
from __future__ import annotations

import datetime as dt
import json
import zoneinfo
import re
import xml.etree.ElementTree as ET
from collections import Counter
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

from lib import bamberg_news

BERLIN = zoneinfo.ZoneInfo("Europe/Berlin")
USER_AGENT = "BambergNewsCollector/1.0 (personal news service; t.boris@gmail.com)"
LAT, LON = 49.8917, 10.8917  # Bamberg
EVIDENCE_DIR = bamberg_news.DEFAULT_WORKSPACE / "state" / "evidence"
STATS_PATH = bamberg_news.DEFAULT_WORKSPACE / "state" / "source-stats.json"
ADAPTIVE_PATH = bamberg_news.DEFAULT_WORKSPACE / "state" / "adaptive-feeds.json"

CORE_FEEDS = {
    "stadt-bamberg-presse": "https://www.stadt.bamberg.de/media/rss/Pressemitteilungen.xml",
    "google-news-bamberg": (
        "https://news.google.com/rss/search?q=Bamberg%20when:2d&hl=de&gl=DE&ceid=DE:de"
    ),
    "google-news-polizei-landkreis": (
        "https://news.google.com/rss/search?q=%22Polizei%20Bamberg%22%20OR%20%22Landkreis%20Bamberg%22%20when:2d&hl=de&gl=DE&ceid=DE:de"
    ),
    "tagesschau-welt": "https://www.tagesschau.de/xml/rss2/",
    "google-news-gesetze": (
        "https://news.google.com/rss/search?q=Gesetzes%C3%A4nderung%20OR%20%22neue%20Regeln%22%20OR%20%22tritt%20in%20Kraft%22%20(Rente%20OR%20Krankenkasse%20OR%20Bayern%20OR%20Deutschland)%20when:7d&hl=de&gl=DE&ceid=DE:de"
    ),
    "google-news-wissenschaft": (
        "https://news.google.com/rss/search?q=Wissenschaft%20OR%20Forschung%20Durchbruch%20when:2d&hl=de&gl=DE&ceid=DE:de"
    ),
    "spektrum-wissenschaft": "https://www.spektrum.de/alias/rss/spektrum-de-rss-feed/996406",
    "google-news-europa": (
        "https://news.google.com/rss/search?q=(EU%20OR%20Europa)%20(Beschluss%20OR%20Gipfel%20OR%20Parlament%20OR%20Kommission)%20when:2d&hl=de&gl=DE&ceid=DE:de"
    ),
    "google-news-herz-kardio": (
        "https://news.google.com/rss/search?q=(Herz%20OR%20Kardiologie%20OR%20Herzschrittmacher%20OR%20Herzinsuffizienz)%20(Studie%20OR%20Forschung)%20when:7d&hl=de&gl=DE&ceid=DE:de"
    ),
    "google-news-rente": (
        "https://news.google.com/rss/search?q=(Rente%20OR%20Rentner)%20(Hinzuverdienst%20OR%20Minijob%20OR%20Steuer%20OR%20Erh%C3%B6hung%20OR%20%22neue%20Regeln%22)%20when:7d&hl=de&gl=DE&ceid=DE:de"
    ),
    "google-news-eigentuemer": (
        "https://news.google.com/rss/search?q=(Grundsteuer%20OR%20Nebenkosten%20OR%20Eigent%C3%BCmer%20OR%20Wohngeb%C3%A4udeversicherung)%20(Bayern%20OR%20Deutschland)%20when:7d&hl=de&gl=DE&ceid=DE:de"
    ),
    "google-news-senioren-rabatte": (
        "https://news.google.com/rss/search?q=(Seniorenrabatt%20OR%20%22Rabatt%20f%C3%BCr%20Senioren%22%20OR%20Seniorenticket)%20when:14d&hl=de&gl=DE&ceid=DE:de"
    ),
    "google-news-flohmarkt-bamberg": (
        "https://news.google.com/rss/search?q=(Flohmarkt%20OR%20Tr%C3%B6delmarkt%20OR%20Sperrm%C3%BCll)%20(Bamberg%20OR%20Oberfranken)%20when:14d&hl=de&gl=DE&ceid=DE:de"
    ),
    "google-news-russischsprachig": (
        "https://news.google.com/rss/search?q=(russischsprachig%20OR%20%22russische%20Gemeinde%22%20OR%20Sp%C3%A4taussiedler)%20(Bamberg%20OR%20Bayern%20OR%20Franken)%20when:14d&hl=de&gl=DE&ceid=DE:de"
    ),
    "google-news-diabetes-blutdruck-migraene": (
        "https://news.google.com/rss/search?q=(Diabetes%20OR%20Pr%C3%A4diabetes%20OR%20Bluthochdruck%20OR%20Migr%C3%A4ne)%20(Studie%20OR%20Forschung)%20when:7d&hl=de&gl=DE&ceid=DE:de"
    ),
}

# How many citations within the window promote a domain to the fetch list.
# Infrastructure/attribution domains never become news feeds.
ADAPTIVE_EXCLUDE = {"commons.wikimedia.org", "dwd.de", "brightsky.dev", "api.brightsky.dev",
                    "openstreetmap.org", "news.google.com", "youtube.com", "youtu.be"}
ADAPTIVE_MIN_CITATIONS = 3
ADAPTIVE_WINDOW_DAYS = 14
MAX_ITEMS_PER_FEED = 25


def _get(url: str, timeout: int = 20) -> requests.Response:
    return requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)


# ------------------------------------------------------------------ weather

def fetch_weather() -> dict[str, Any]:
    """Bright Sky (DWD data): current conditions, 3-day min/max, official alerts."""
    out: dict[str, Any] = {"source": "DWD via brightsky.dev", "days": [], "alerts": []}
    try:
        current = _get(f"https://api.brightsky.dev/current_weather?lat={LAT}&lon={LON}").json()
        w = current.get("weather", {})
        out["current"] = {
            "temperature": w.get("temperature"),
            "condition": w.get("condition"),
            "icon": w.get("icon"),
            "wind_speed": w.get("wind_speed_30") or w.get("wind_speed_10"),
            "humidity": w.get("relative_humidity"),
            "pressure": round(w["pressure_msl"]) if w.get("pressure_msl") else None,
        }
    except Exception as exc:  # noqa: BLE001 - a pack must degrade, not die
        out["current_error"] = str(exc)
    today = dt.datetime.now(BERLIN).date()
    for offset in range(3):
        day = today + dt.timedelta(days=offset)
        try:
            data = _get(
                f"https://api.brightsky.dev/weather?lat={LAT}&lon={LON}&date={day.isoformat()}"
            ).json()
            hours = data.get("weather", [])
            temps = [h["temperature"] for h in hours if h.get("temperature") is not None]
            rain = sum(h.get("precipitation") or 0 for h in hours)
            icons = Counter(h.get("icon") for h in hours if h.get("icon"))
            hums = [h["relative_humidity"] for h in hours if h.get("relative_humidity") is not None]
            pressures = [h["pressure_msl"] for h in hours if h.get("pressure_msl") is not None]
            if temps:
                out["days"].append(
                    {
                        "date": day.isoformat(),
                        "min": round(min(temps), 1),
                        "max": round(max(temps), 1),
                        "precipitation_mm": round(rain, 1),
                        "humidity": round(sum(hums) / len(hums)) if hums else None,
                        "pressure": round(sum(pressures) / len(pressures)) if pressures else None,
                        "dominant_icon": icons.most_common(1)[0][0] if icons else None,
                    }
                )
        except Exception as exc:  # noqa: BLE001
            out.setdefault("day_errors", []).append({"date": day.isoformat(), "error": str(exc)})
    try:
        alerts = _get(f"https://api.brightsky.dev/alerts?lat={LAT}&lon={LON}").json()
        for alert in alerts.get("alerts", []):
            out["alerts"].append(
                {
                    "headline": alert.get("headline_de") or alert.get("headline_en"),
                    "severity": alert.get("severity"),
                    "onset": alert.get("onset"),
                    "expires": alert.get("expires"),
                    "description": (alert.get("description_de") or "")[:400],
                }
            )
    except Exception as exc:  # noqa: BLE001
        out["alerts_error"] = str(exc)
    return out


# -------------------------------------------------------------------- feeds

def _text(node: ET.Element | None) -> str:
    return (node.text or "").strip() if node is not None else ""


def fetch_feed(url: str) -> list[dict[str, str]]:
    resp = _get(url)
    resp.raise_for_status()
    root = ET.fromstring(resp.content)
    entries = []
    for item in root.iter("item"):
        source = item.find("source")
        entries.append(
            {
                "title": _text(item.find("title")),
                "link": _text(item.find("link")),
                "published": _text(item.find("pubDate")),
                "publisher": _text(source) or urlparse(_text(item.find("link"))).netloc,
            }
        )
        if len(entries) >= MAX_ITEMS_PER_FEED:
            break
    return entries


# --------------------------------------------------- adaptive source learning

def _domain(url: str) -> str:
    netloc = urlparse(url).netloc.lower()
    return netloc[4:] if netloc.startswith("www.") else netloc


def update_source_stats() -> dict[str, Any]:
    """Count cited domains across recent episodes; promote hot ones to feeds."""
    cutoff = dt.datetime.now(BERLIN).date() - dt.timedelta(days=ADAPTIVE_WINDOW_DAYS)
    counts: Counter[str] = Counter()
    runs_root = bamberg_news.runs_root()
    for episode_path in sorted(runs_root.glob("*/episode.json")):
        try:
            episode = json.loads(episode_path.read_text(encoding="utf-8"))
            as_of = str(episode.get("as_of", ""))[:10]
            if as_of and as_of < cutoff.isoformat():
                continue
            for source in episode.get("sources", []):
                url = str(source.get("url", ""))
                if url.startswith("http"):
                    counts[_domain(url)] += 1
        except (OSError, ValueError):
            continue
    stats = {
        "updated_at": dt.datetime.now().astimezone().isoformat(),
        "window_days": ADAPTIVE_WINDOW_DAYS,
        "citations": dict(counts.most_common()),
    }
    STATS_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATS_PATH.write_text(json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")

    adaptive = {}
    if ADAPTIVE_PATH.is_file():
        adaptive = json.loads(ADAPTIVE_PATH.read_text(encoding="utf-8"))
    known = {_domain(u) for u in CORE_FEEDS.values()} | {
        _domain(f["feed"]) for f in adaptive.values() if isinstance(f, dict) and f.get("feed")
    }
    promoted = []
    for domain, count in counts.items():
        if count < ADAPTIVE_MIN_CITATIONS or domain in known or domain in adaptive or domain in ADAPTIVE_EXCLUDE:
            continue
        feed = discover_feed(domain)
        adaptive[domain] = {
            "citations": count,
            "feed": feed,
            "discovered_at": dt.datetime.now().astimezone().isoformat(),
        }
        if feed:
            promoted.append({"domain": domain, "feed": feed, "citations": count})
    ADAPTIVE_PATH.write_text(json.dumps(adaptive, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"citations": dict(counts.most_common(10)), "promoted_now": promoted}


def discover_feed(domain: str) -> str | None:
    """Find an RSS feed for a domain: <link rel=alternate> then common paths."""
    base = f"https://{domain}"
    try:
        html = _get(base, timeout=15).text[:200_000]
        match = re.search(
            r'<link[^>]+type="application/(?:rss|atom)\+xml"[^>]+href="([^"]+)"', html
        ) or re.search(
            r'<link[^>]+href="([^"]+)"[^>]+type="application/(?:rss|atom)\+xml"', html
        )
        if match:
            href = match.group(1)
            if href.startswith("http"):
                return href
            return base + (href if href.startswith("/") else "/" + href)
    except Exception:  # noqa: BLE001
        pass
    for path in ("/rss", "/feed", "/rss.xml", "/index.rss"):
        try:
            resp = _get(base + path, timeout=10)
            if resp.status_code == 200 and b"<rss" in resp.content[:2000]:
                return base + path
        except Exception:  # noqa: BLE001
            continue
    return None


# AI search layer (Boris 2026-07-20: Perplexity sweep — candidates only; the
# worker must verify every fact against the cited publisher pages).
PPLX_API = "https://api.perplexity.ai/chat/completions"
PPLX_MODEL = "sonar"
PPLX_QUERIES: dict[str, str] = {
    "bamberg": "Wichtigste verifizierte Nachrichten aus Bamberg und Landkreis Bamberg der letzten 24 Stunden",
    "franken": "Wichtige Nachrichten aus Oberfranken und Bayern der letzten 24 Stunden",
    "germany": "Top verifizierte Deutschland-Nachrichten der letzten 24 Stunden",
    "laws-pensioners": "Neue deutsche Gesetze, Renten- und Steueränderungen, die Rentner und Wohnungseigentümer betreffen — letzte 7 Tage",
    "europe": "Top verified European news of the last 24 hours",
    "world": "Top verified world news of the last 12 hours",
    "ukraine": "Latest verified factual developments in the Russia-Ukraine war in the last 24 hours — facts and numbers only",
    "medicine-heart": "New peer-reviewed research on cardiology, pacemakers, heart failure — last 7 days",
    "medicine-common": "New research on pre-diabetes, hypertension, migraines — last 7 days",
    "senior-life": "Seniorenrabatte, Flohmärkte in Bamberg und Umgebung, Veranstaltungen für Senioren — kommende Woche",
    "science": "Most significant peer-reviewed science news of the last 48 hours",
}


def _perplexity_key() -> str | None:
    try:
        cfg = json.loads((Path.home() / ".openclaw" / "openclaw.json").read_text(encoding="utf-8"))
        return cfg["plugins"]["entries"]["perplexity"]["config"]["webSearch"]["apiKey"]
    except (OSError, KeyError, ValueError):
        return None


def perplexity_search(query: str, api_key: str) -> dict[str, Any]:
    resp = requests.post(
        PPLX_API,
        headers={"Authorization": f"Bearer {api_key}", "User-Agent": USER_AGENT},
        json={
            "model": PPLX_MODEL,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are a news scout. Report only stories you can cite with a source URL. "
                        "For each story: headline, one factual sentence, publisher. No opinion."
                    ),
                },
                {"role": "user", "content": f"{query}. List up to 6 stories."},
            ],
            "max_tokens": 700,
        },
        timeout=90,
    )
    resp.raise_for_status()
    data = resp.json()
    return {
        "summary": (data.get("choices") or [{}])[0].get("message", {}).get("content", ""),
        "results": [
            {"title": r.get("title", ""), "url": r.get("url", ""), "date": r.get("date", "")}
            for r in (data.get("search_results") or data.get("citations") or [])
            if isinstance(r, dict)
        ][:10],
    }


# --------------------------------------------------------------------- pack

def collect(slot: str) -> dict[str, Any]:
    if slot not in ("morning", "evening"):
        raise bamberg_news.BambergNewsError(f"invalid slot: {slot}")
    adaptive_feeds: dict[str, str] = {}
    if ADAPTIVE_PATH.is_file():
        for domain, meta in json.loads(ADAPTIVE_PATH.read_text(encoding="utf-8")).items():
            if isinstance(meta, dict) and meta.get("feed"):
                adaptive_feeds[f"adaptive-{domain}"] = meta["feed"]
    pack: dict[str, Any] = {
        "schema_version": "bamberg-news-evidence/v1",
        "generated_at": dt.datetime.now().astimezone().isoformat(),
        "slot": slot,
        "weather": fetch_weather(),
        "feeds": {},
        "feed_errors": {},
    }
    for name, url in {**CORE_FEEDS, **adaptive_feeds}.items():
        try:
            pack["feeds"][name] = fetch_feed(url)
        except Exception as exc:  # noqa: BLE001
            pack["feed_errors"][name] = str(exc)
    pplx_key = _perplexity_key()
    pack["ai_search"] = {}
    if pplx_key:
        for topic, query in PPLX_QUERIES.items():
            try:
                pack["ai_search"][topic] = perplexity_search(query, pplx_key)
            except Exception as exc:  # noqa: BLE001
                pack["feed_errors"][f"pplx-{topic}"] = str(exc)
    else:
        pack["feed_errors"]["pplx"] = "no perplexity api key in openclaw.json"
    try:
        pack["adaptive"] = update_source_stats()
    except Exception as exc:  # noqa: BLE001
        pack["adaptive_error"] = str(exc)
    EVIDENCE_DIR.mkdir(parents=True, exist_ok=True)
    out_path = EVIDENCE_DIR / f"{dt.datetime.now(BERLIN).date().isoformat()}-{slot}.json"
    out_path.write_text(json.dumps(pack, indent=2, ensure_ascii=False), encoding="utf-8")
    pack["evidence_path"] = str(out_path)
    return pack
