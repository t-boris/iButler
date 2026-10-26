"""What the Mac itself is doing, as metadata for the addressee decision (REQ-036).

Only the foreground application's name and bundle id and whether audio is
playing from the Mac are sensed. Window titles, text and screenshots are never
read here: the decision needs to know that a call is on or music is playing,
not what is being said or shown.
"""
from __future__ import annotations

import math
import re
import time
from array import array

from . import processes as subprocess

CALL_APPS = {"us.zoom.xos", "com.microsoft.teams2", "com.microsoft.teams", "com.apple.FaceTime", "com.skype.skype",
             "com.cisco.webexmeetingsapp"}      # a messenger in front is chat; a call in it shows up as audio playing
AUDIO_FLOOR_DBFS = -45.0        # quieter than this from the Mac's output counts as silence
AUDIO_FRESH_S = 3.0


def foreground_app(timeout=1.0):
    """Name and bundle id of the frontmost app via lsappinfo (no automation consent needed)."""
    try:
        front = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True, timeout=timeout)
        asn = front.stdout.strip()
        if front.returncode or not asn:
            return {}
        info = subprocess.run(["lsappinfo", "info", "-only", "name,bundleid", asn], capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return {}
    name = re.search(r'^"([^"\n]{1,80})"', info.stdout, re.M)
    bundle = re.search(r'bundleID="([^"\n]{1,160})"', info.stdout)
    result = {}
    if name: result["name"] = "".join(ch for ch in name.group(1) if ch.isprintable() and not ch.isspace()).strip() or name.group(1).strip()
    if bundle: result["bundle_id"] = bundle.group(1)
    return result


def audio_level_dbfs(pcm):
    samples = array("h", pcm[: len(pcm) - len(pcm) % 2])
    if not samples:
        return -120.0
    rms = math.sqrt(sum(float(x) * x for x in samples) / len(samples)) / 32768
    return round(20 * math.log10(max(rms, 1e-6)), 1)


def desktop_state(service, now=None):
    """Foreground app, call app, audio playing: three facts, or an honest 'unknown'."""
    now = time.time() if now is None else now
    app = foreground_app()
    archive = getattr(service, "archive", None)
    last = (getattr(archive, "last", None) or {}).get("system-audio")
    level = getattr(archive, "system_dbfs", None)
    fresh = isinstance(last, (int, float)) and isinstance(level, (int, float)) and 0 <= now - last <= AUDIO_FRESH_S
    state = {"foreground_app": app.get("name"), "foreground_bundle": app.get("bundle_id"),
             "call_app": app.get("bundle_id") in CALL_APPS if app.get("bundle_id") else None,
             "audio_playing": (level > AUDIO_FLOOR_DBFS) if fresh else None,
             "known": bool(app) and fresh}
    return state


def quiet_alone(alone, desktop):
    """The owner is the default addressee only when alone AND the desktop is known to be quiet."""
    return bool(alone and desktop.get("known") and desktop.get("call_app") is False and desktop.get("audio_playing") is False)
