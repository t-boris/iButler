"""Curated openly-licensed Bamberg b-roll library (Wikimedia Commons).

Downloads freely licensed photos of Bamberg into the workspace b-roll library
with full attribution metadata. Scenes reference them via ``visual.mode="broll"``
(the renderer turns stills into Ken Burns clips); the worker copies the chosen
files into the draft's own ``assets/broll/`` directory and records the credit
line from the manifest in ``credits.md``.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import re
from pathlib import Path
from typing import Any

import requests

from lib import bamberg_news

BROLL_DIR = bamberg_news.DEFAULT_WORKSPACE / "assets" / "broll"
MANIFEST_PATH = BROLL_DIR / "manifest.json"
API = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "BambergNewsBroll/1.0 (personal news service; t.boris@gmail.com)"

FREE_LICENSES = re.compile(r"^(cc0|cc[ -]by(?:[ -]sa)?(?:[ -]\d\.\d)?|public domain|pd)", re.I)

THEMES: dict[str, str] = {
    "altstadt": "Bamberg Altes Rathaus",
    "dom": "Bamberg Dom cathedral",
    "klein-venedig": "Klein Venedig Bamberg",
    "regnitz": "Regnitz Bamberg river",
    "panorama": "Bamberg panorama city view",
    "michaelsberg": "Kloster Michaelsberg Bamberg",
    "market": "Bamberg Grüner Markt",
    "transport": "Bamberg Bahnhof",
    "garden": "Bamberg Hain park",
    "winter": "Bamberg winter snow",
    # Thematic stock (Boris 2026-07-20: every topic needs a picture)
    "money-pension": "euro coins banknotes savings",
    "seniors": "elderly couple walking park",
    "medicine": "blood pressure monitor stethoscope",
    "health-heart": "cardiology heart model hospital",
    "property": "Franconia half-timbered house facade",
    "flea-market": "flea market Flohmarkt stalls",
    "police": "Polizei Germany police car",
    "firefighters": "Feuerwehr Germany fire engine",
    "bus-transport": "Stadtbus Germany city bus",
    "eu-politics": "European Union flags Brussels",
    "germany-politics": "Bundestag Reichstag building",
    "science-lab": "laboratory research microscope",
    "weather-rain": "rain umbrella street",
    "weather-sun": "summer sun heat city",
    # Story-type coverage (Boris 2026-07-20: «картинки для всех тем новостей»)
    "kyiv": "Kyiv Ukraine city",
    "kremlin": "Moscow Kremlin",
    "hospital": "Krankenhaus hospital building Germany",
    "pharmacy": "Apotheke pharmacy Germany",
    "supermarket": "supermarket Germany shelves",
    "deutsche-bahn": "Deutsche Bahn ICE train",
    "energy": "power lines electricity pylon Germany",
    "school": "Schule school building Germany",
    "courthouse": "Gericht courthouse Germany",
    "construction": "Baustelle construction crane Germany",
    "weather-storm": "thunderstorm dark clouds lightning",
    "garden-flowers": "Kleingarten allotment garden Germany",
}
PER_THEME = 5


def _api(params: dict[str, Any]) -> dict[str, Any]:
    resp = requests.get(
        API,
        params={"format": "json", "action": "query", **params},
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _clean(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", value or "")).strip()


def build_library(themes: dict[str, str] | None = None, per_theme: int = PER_THEME) -> dict[str, Any]:
    themes = themes or THEMES
    BROLL_DIR.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []
    if MANIFEST_PATH.is_file():
        manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    have = {entry["file"] for entry in manifest}
    added, skipped = 0, 0
    for theme, query in themes.items():
        data = _api(
            {
                "generator": "search",
                "gsrsearch": f"filetype:bitmap {query}",
                "gsrnamespace": 6,
                "gsrlimit": per_theme * 4,
                "prop": "imageinfo",
                "iiprop": "url|extmetadata|size",
                "iiurlwidth": 1920,
            }
        )
        pages = (data.get("query") or {}).get("pages") or {}
        count = 0
        for page in pages.values():
            if count >= per_theme:
                break
            info = (page.get("imageinfo") or [{}])[0]
            meta = info.get("extmetadata") or {}
            license_name = _clean((meta.get("LicenseShortName") or {}).get("value", ""))
            if not FREE_LICENSES.match(license_name):
                skipped += 1
                continue
            width, height = info.get("width", 0), info.get("height", 0)
            if width < 1200 or height < 700:
                skipped += 1
                continue
            url = info.get("thumburl") or info.get("url")
            if not url:
                continue
            slug = re.sub(r"[^a-z0-9]+", "-", page.get("title", "").lower())[:60].strip("-")
            filename = f"{theme}--{slug}.jpg"
            if filename in have:
                continue
            try:
                import time
                time.sleep(0.6)  # stay under Wikimedia rate limits (2026-07-20: 427 HTML error pages saved as .jpg)
                resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=60)
                if resp.status_code == 429:
                    time.sleep(10)
                    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=60)
                blob = resp.content
            except Exception:  # noqa: BLE001
                continue
            if resp.status_code != 200 or not (blob[:3] == b"\xff\xd8\xff" or blob[:4] == b"\x89PNG"):
                skipped += 1
                continue
            (BROLL_DIR / filename).write_bytes(blob)
            manifest.append(
                {
                    "file": filename,
                    "theme": theme,
                    "title": _clean(page.get("title", "").replace("File:", "")),
                    "author": _clean((meta.get("Artist") or {}).get("value", ""))[:120],
                    "license": license_name,
                    "source_url": info.get("descriptionurl") or url,
                    "added_at": dt.datetime.now().astimezone().isoformat(),
                }
            )
            have.add(filename)
            added += 1
            count += 1
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    return {"library": str(BROLL_DIR), "total": len(manifest), "added": added, "skipped_non_free_or_small": skipped}


def list_library(theme: str | None = None) -> list[dict[str, Any]]:
    if not MANIFEST_PATH.is_file():
        return []
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if theme:
        manifest = [entry for entry in manifest if entry["theme"] == theme]
    return manifest
