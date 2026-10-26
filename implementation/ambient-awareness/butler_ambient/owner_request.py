"""Resolve an actual recent owner message; never trust model-supplied requests."""
import datetime as dt
import json
from pathlib import Path

OWNER = "253483520"
SESSION_KEY = "agent:butler:telegram:direct:" + OWNER


def latest(root=None, now=None):
    root = Path(root or Path.home() / ".openclaw/agents/butler/sessions").resolve()
    now = dt.datetime.now(dt.timezone.utc).timestamp() if now is None else now
    record = json.loads((root / "sessions.json").read_text())[SESSION_KEY]
    delivery, origin = record.get("deliveryContext", {}), record.get("origin", {})
    if (delivery != {"channel": "telegram", "to": "telegram:" + OWNER, "accountId": "butler"}
            or origin.get("from") != "telegram:" + OWNER or origin.get("to") != "telegram:" + OWNER
            or origin.get("accountId") != "butler" or origin.get("chatType") != "direct"):
        raise ValueError("owner route unavailable")
    path = Path(record["sessionFile"])
    if path.is_symlink() or path.resolve().parent != root or path.suffix != ".jsonl":
        raise ValueError("owner session unavailable")
    with path.open("rb") as stream:
        size = stream.seek(0, 2)
        stream.seek(max(0, size - 262144))
        data = stream.read(262144)
    lines = data.splitlines()
    if size > 262144: lines = lines[1:]
    for line in reversed(lines):
        try: value = json.loads(line)
        except (ValueError, UnicodeDecodeError): continue
        message = value.get("message", {})
        if message.get("role") != "user": continue
        # A newer non-Telegram or stale user message invalidates the old one.
        at = dt.datetime.fromisoformat(value["timestamp"].replace("Z", "+00:00")).timestamp()
        text, key = message.get("content"), value.get("id")
        if (message.get("sourceChannel") != "telegram" or not 0 <= now - at <= 600 or
                not isinstance(text, str) or not 1 <= len(text) <= 8000 or
                not isinstance(key, str) or not 1 <= len(key) <= 100):
            raise ValueError("no fresh explicit owner message")
        return {"id": "telegram:" + key, "text": text, "at": at}
    raise ValueError("no fresh explicit owner message")
