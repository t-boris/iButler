"""Static OpenStreetMap snippets for road-closure / location scenes.

Composes standard OSM raster tiles around a geocoded Bamberg address, draws a
pin, and burns in the mandatory "© OpenStreetMap contributors" attribution
(ODbL). Output is referenced from scenes via ``visual.mode="image"`` (or
``broll`` for a Ken Burns pan over the map).
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageDraw, ImageFont

USER_AGENT = "BambergNewsMap/1.0 (personal news service; t.boris@gmail.com)"
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
NOMINATIM = "https://nominatim.openstreetmap.org/search"
FONT_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
BAMBERG_VIEWBOX = "10.83,49.93,10.96,49.85"  # keep geocoding local


class MapError(RuntimeError):
    pass


def geocode(query: str) -> tuple[float, float, str]:
    resp = requests.get(
        NOMINATIM,
        params={
            "q": query,
            "format": "json",
            "limit": 1,
            "viewbox": BAMBERG_VIEWBOX,
            "bounded": 0,
            "countrycodes": "de",
        },
        headers={"User-Agent": USER_AGENT},
        timeout=30,
    )
    resp.raise_for_status()
    rows = resp.json()
    if not rows:
        raise MapError(f"no geocoding result for {query!r}")
    row = rows[0]
    return float(row["lat"]), float(row["lon"]), row.get("display_name", query)


def _deg2xy(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    n = 2.0**zoom
    x = (lon + 180.0) / 360.0 * n
    y = (1.0 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2.0 * n
    return x, y


def render_map(
    query: str,
    out_path: str | Path,
    *,
    zoom: int = 16,
    # Landscape by default (Boris 2026-08-01): the map fills the 16:9 card's
    # media window, where it is shown whole rather than cropped.
    width: int = 1400,
    height: int = 900,
    label: str | None = None,
) -> dict[str, Any]:
    lat, lon, display = geocode(query)
    cx, cy = _deg2xy(lat, lon, zoom)
    tile_size = 256
    canvas = Image.new("RGB", (width + 2 * tile_size, height + 2 * tile_size))
    origin_px = cx * tile_size - canvas.width / 2
    origin_py = cy * tile_size - canvas.height / 2
    x0, y0 = int(origin_px // tile_size), int(origin_py // tile_size)
    x1 = int((origin_px + canvas.width) // tile_size) + 1
    y1 = int((origin_py + canvas.height) // tile_size) + 1
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    for tx in range(x0, x1 + 1):
        for ty in range(y0, y1 + 1):
            try:
                tile = session.get(TILE_URL.format(z=zoom, x=tx, y=ty), timeout=20)
                tile.raise_for_status()
                img = Image.open(__import__("io").BytesIO(tile.content)).convert("RGB")
            except Exception:  # noqa: BLE001 - a missing tile stays gray
                img = Image.new("RGB", (tile_size, tile_size), (228, 228, 224))
            canvas.paste(img, (int(tx * tile_size - origin_px), int(ty * tile_size - origin_py)))
    left = (canvas.width - width) // 2
    top = (canvas.height - height) // 2
    out = canvas.crop((left, top, left + width, top + height))
    draw = ImageDraw.Draw(out)
    px, py = width / 2, height / 2
    r = max(10, width // 54)
    draw.ellipse((px - r * 2.4, py - r * 2.4, px + r * 2.4, py + r * 2.4), outline=(200, 30, 40), width=6)
    draw.polygon([(px - r, py - r * 0.2), (px + r, py - r * 0.2), (px, py + r * 1.8)], fill=(200, 30, 40))
    draw.ellipse((px - r, py - r * 1.4, px + r, py + r * 0.6), fill=(200, 30, 40))
    draw.ellipse((px - r * 0.4, py - r * 0.8, px + r * 0.4, py), fill=(255, 255, 255))
    if label:
        font = ImageFont.truetype(FONT_BOLD, max(26, width // 30))
        tw = draw.textbbox((0, 0), label, font=font)[2]
        bx = (width - tw) / 2
        draw.rounded_rectangle((bx - 18, 24, bx + tw + 18, 24 + font.size * 1.7), radius=14, fill=(7, 12, 22, 235))
        draw.text((bx, 24 + font.size * 0.28), label, font=font, fill=(255, 255, 255))
    attr_font = ImageFont.truetype(FONT_BOLD, max(18, width // 48))
    attr = "© OpenStreetMap contributors"
    tw = draw.textbbox((0, 0), attr, font=attr_font)[2]
    draw.rectangle((width - tw - 26, height - attr_font.size * 1.9, width, height), fill=(255, 255, 255))
    draw.text((width - tw - 14, height - attr_font.size * 1.6), attr, font=attr_font, fill=(60, 60, 60))
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.save(out_path, quality=90)
    return {"map": str(out_path), "lat": lat, "lon": lon, "matched": display, "zoom": zoom}
