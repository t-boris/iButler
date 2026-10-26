"""Owner-selected synthetic voice profiles; no owner-voice cloning."""
from __future__ import annotations

import json
from . import processes as subprocess


def profiles(store):
    root = store.root / "voice"
    manifest = root / "profiles.json"
    if not manifest.is_file():
        return []
    value = json.loads(manifest.read_text())
    result = []
    if value.get("synthetic") is not True:
        return []
    for item in value.get("profiles", []):
        key = item.get("id")
        if key not in {"calm", "baritone", "warm"}:
            continue
        path = root / "previews" / (key + ".wav")
        if path.is_file() and not path.is_symlink():
            result.append({"id": key, "name": item["name"], "file": str(path)})
    return result


def select(store, key):
    if key not in {p["id"] for p in profiles(store)}:
        raise ValueError("preview is unavailable")
    store.set("voice_profile", key)
    store.set("voice_enabled", True)
    return {"selected": key, "synthetic": True}


def preview(store, key):
    value = next((p for p in profiles(store) if p["id"] == key), None)
    if value is None:
        raise ValueError("preview is unavailable")
    # This is a fixed synthetic demonstration explicitly requested from the UI.
    # It contains no ambient observation and does not activate conversation.
    subprocess.run(["/usr/bin/afplay", value["file"]], check=True, timeout=90)
    return {"played": key, "synthetic": True, "voice_activated": False}
