"""Curated openly-licensed b-roll library for AI Butler News (Wikimedia Commons).

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

BROLL_DIR = Path("/Users/boris/.openclaw/workspace-butler") / "assets" / "broll"
MANIFEST_PATH = BROLL_DIR / "manifest.json"
API = "https://commons.wikimedia.org/w/api.php"
USER_AGENT = "AIButlerNewsBroll/1.0 (personal news service; t.boris@gmail.com)"

FREE_LICENSES = re.compile(r"^(cc0|cc[ -]by(?:[ -]sa)?(?:[ -]\d\.\d)?|public domain|pd)", re.I)

THEMES: dict[str, str] = {
    "gurnee": "Gurnee Illinois",
    "lake-county": "Lake County Illinois",
    "chicago": "Chicago skyline",
    "illinois": "Illinois State Capitol Springfield",
    "us-capitol": "United States Capitol Washington",
    "white-house": "White House Washington",
    "world": "United Nations headquarters flags",
    "ukraine": "Kyiv Ukraine city",
    "ai-datacenter": "data center server racks",
    "ai-chip": "microprocessor CPU chip",
    "developer": "software developer computer code",
    "science-lab": "laboratory research microscope",
    "space": "rocket launch space",
    "business": "office skyscrapers downtown",
    "wall-street": "Wall Street New York Stock Exchange",
    "crypto": "bitcoin cryptocurrency coin",
    "money": "dollar banknotes cash",
    "gas-station": "gas station fuel pumps USA",
    # Story-type coverage (Boris 2026-07-20: «картинки для всех тем новостей»)
    "police": "police car United States lights",
    "fire-rescue": "fire engine firefighters USA",
    "courthouse": "courthouse columns United States",
    "school": "school building classroom USA",
    "hospital": "hospital building emergency entrance USA",
    "medicine": "stethoscope doctor medical",
    "weather-storm": "thunderstorm dark clouds lightning",
    "weather-snow": "snow storm street winter USA",
    "weather-heat": "hot summer sun sky",
    "flood": "flood street water houses",
    "wildfire": "wildfire smoke forest fire",
    "highway": "interstate highway traffic USA",
    "airport": "airport terminal airplane gate",
    "train": "Metra commuter train Chicago",
    "housing": "suburban houses residential street USA",
    "construction": "construction site crane building",
    "retail": "supermarket grocery store shelves",
    "factory": "factory manufacturing plant workers",
    "energy": "power lines electricity pylon grid",
    "wind-solar": "wind turbines solar panels field",
    "farm": "Illinois farm cornfield barn",
    "election": "voting ballot election USA",
    "supreme-court": "Supreme Court building Washington",
    "military": "United States military soldiers vehicles",
    "nato": "NATO headquarters flags",
    "kremlin": "Moscow Kremlin",
    "ev-car": "electric car charging station",
    "robot": "humanoid robot robotics lab",
    "smartphone": "smartphone hands apps screen",
    "cybersecurity": "computer security padlock code",
    "federal-reserve": "Federal Reserve building Washington",
    "bank": "bank building facade USA",
    "startup": "office meeting laptops team",
    "pharma": "medicine pills tablets",
    "stadium": "American football stadium crowd",
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
